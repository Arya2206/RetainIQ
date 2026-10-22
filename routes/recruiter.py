from collections import Counter
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from extensions import db, utcnow
from models import (Opportunity, Application, Interview, Offer, Department, User, notify,
                    APPLICATION_FLOW, APPLICATION_LABELS)
from security import permission_required, log_action
from utils import roles_required

bp = Blueprint("recruiter", __name__, url_prefix="/recruitment")


@bp.before_request
@login_required
@roles_required("recruiter", "hr", "super_admin")
def guard():
    pass


def _when(dt):
    d = (dt.date() - utcnow().date()).days
    word = "today" if d == 0 else "tomorrow" if d == 1 else dt.strftime("%d %b")
    return f"{word} at {dt.strftime('%H:%M')}"


@bp.route("/")
def dashboard():
    jobs = Opportunity.query.order_by(Opportunity.created_at.desc()).all()
    funnel = Counter(a.status for a in Application.query.all())
    return render_template(
        "recruiter/dashboard.html", jobs=jobs, total_apps=Application.query.count(),
        pending_interviews=Interview.query.filter_by(result="pending").count(),
        open_offers=Offer.query.filter_by(status="pending").count(),
        funnel=[(APPLICATION_LABELS[s], funnel.get(s, 0)) for s in APPLICATION_FLOW])


@bp.route("/opportunities/create", methods=["GET", "POST"])
@permission_required("manage_recruitment")
def create_opportunity():
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()[:150]
        if not title:
            flash("A job title is required.", "danger")
            return redirect(url_for("recruiter.create_opportunity"))
        o = Opportunity(title=title, description=f.get("description", "")[:4000],
                        department_id=f.get("department_id") or None,
                        required_skills=",".join(s.strip() for s in f.get("skills", "").split(",") if s.strip())[:255],
                        posted_by=current_user.id, is_internal=f.get("is_internal") == "on")
        db.session.add(o)
        db.session.flush()
        for u in User.query.filter_by(role="employee", is_active_flag=True).all():
            notify(u.id, f"New internal opportunity: {o.title}", "/employee/opportunities", "application")
        log_action(f"Posted opportunity '{o.title}'", "hiring")
        db.session.commit()
        flash("Opportunity posted and employees have been notified.", "success")
        return redirect(url_for("recruiter.dashboard"))
    return render_template("recruiter/create_opportunity.html", departments=Department.query.order_by(Department.name).all())


@bp.route("/opportunities/<int:opp_id>/toggle", methods=["POST"])
@permission_required("manage_recruitment")
def toggle_opportunity(opp_id):
    o = db.get_or_404(Opportunity, opp_id)
    o.status = "closed" if o.status == "open" else "open"
    log_action(f"{o.status.title()} opportunity '{o.title}'", "hiring")
    db.session.commit()
    return redirect(url_for("recruiter.dashboard"))


@bp.route("/opportunities/<int:opp_id>/candidates")
def candidates(opp_id):
    opp = db.get_or_404(Opportunity, opp_id)
    apps = Application.query.filter_by(opportunity_id=opp_id).order_by(Application.applied_at.desc()).all()
    return render_template("recruiter/candidates.html", opp=opp, apps=apps)


@bp.route("/applications/<int:app_id>/review", methods=["POST"])
@permission_required("review_applications")
def review(app_id):
    a = db.get_or_404(Application, app_id)
    new = request.form.get("status")
    allowed = {"submitted": ["under_review", "rejected"], "under_review": ["shortlisted", "rejected"],
               "shortlisted": ["rejected"]}
    if new not in allowed.get(a.status, []):
        flash("That status change is not allowed from the current stage.", "danger")
    else:
        a.status = new
        msg = {"under_review": "is now under review", "shortlisted": "has been shortlisted",
               "rejected": "was not taken forward"}[new]
        notify(a.applicant_id, f"Your application for '{a.opportunity.title}' {msg}.", "/employee/applications", "application")
        log_action(f"Application #{a.id} → {new}", "hiring")
        db.session.commit()
        flash("Application updated.", "success")
    return redirect(url_for("recruiter.candidates", opp_id=a.opportunity_id))


@bp.route("/applications/<int:app_id>/interview", methods=["POST"])
@permission_required("schedule_interviews")
def schedule_interview(app_id):
    a = db.get_or_404(Application, app_id)
    if a.status not in ("shortlisted", "interview"):
        flash("Shortlist the candidate before scheduling an interview.", "danger")
        return redirect(url_for("recruiter.candidates", opp_id=a.opportunity_id))
    try:
        when = datetime.fromisoformat(request.form.get("scheduled_at") or "")
    except ValueError:
        when = utcnow() + timedelta(days=1, hours=1)
    iv = Interview(application_id=a.id, interviewer=request.form.get("interviewer", "TBD")[:120] or "TBD", scheduled_at=when)
    db.session.add(iv)
    a.status = "interview"
    notify(a.applicant_id, f"Interview scheduled for '{a.opportunity.title}' {_when(when)}.", "/employee/interviews", "interview")
    log_action(f"Scheduled interview for application #{a.id}", "hiring")
    db.session.commit()
    flash("Interview scheduled and the candidate was notified.", "success")
    return redirect(url_for("recruiter.candidates", opp_id=a.opportunity_id))


@bp.route("/interviews/<int:iv_id>/feedback", methods=["POST"])
@permission_required("schedule_interviews")
def interview_feedback(iv_id):
    iv = db.get_or_404(Interview, iv_id)
    iv.feedback = request.form.get("feedback", "")[:2000]
    iv.result = request.form.get("result") if request.form.get("result") in ("pending", "pass", "fail") else "pending"
    a = iv.application
    if iv.result == "pass":
        a.status = "selected"
        notify(a.applicant_id, f"Congratulations! You've been selected for '{a.opportunity.title}'.", "/employee/applications", "application")
    elif iv.result == "fail":
        a.status = "rejected"
        notify(a.applicant_id, f"Update on your application for '{a.opportunity.title}'.", "/employee/applications", "application")
    log_action(f"Interview #{iv.id} result: {iv.result}", "hiring")
    db.session.commit()
    flash("Interview feedback saved.", "success")
    return redirect(url_for("recruiter.candidates", opp_id=a.opportunity_id))


@bp.route("/applications/<int:app_id>/offer", methods=["POST"])
@permission_required("send_offers")
def make_offer(app_id):
    a = db.get_or_404(Application, app_id)
    try:
        salary = float(request.form.get("offered_salary") or 0)
    except ValueError:
        salary = 0
    if a.status != "selected" or salary <= 0 or a.offer:
        flash("An offer needs a selected candidate and a valid salary.", "danger")
        return redirect(url_for("recruiter.candidates", opp_id=a.opportunity_id))
    db.session.add(Offer(application_id=a.id, offered_salary=salary))
    a.status = "offer"
    notify(a.applicant_id, f"You have received an offer for '{a.opportunity.title}'.", "/employee/offers", "offer")
    log_action(f"Sent offer for application #{a.id}", "hiring")
    db.session.commit()
    flash("Offer sent.", "success")
    return redirect(url_for("recruiter.candidates", opp_id=a.opportunity_id))


@bp.route("/interviews")
def interviews():
    f = request.args.get("result", "")
    q = Interview.query
    if f in ("pending", "pass", "fail"):
        q = q.filter_by(result=f)
    return render_template("recruiter/interviews.html", items=q.order_by(Interview.scheduled_at.desc()).all(), f=f)


@bp.route("/offers")
def offers():
    f = request.args.get("status", "")
    q = Offer.query
    if f in ("pending", "accepted", "declined"):
        q = q.filter_by(status=f)
    return render_template("recruiter/offers.html", items=q.order_by(Offer.created_at.desc()).all(), f=f)
