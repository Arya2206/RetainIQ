from collections import Counter
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from extensions import db, utcnow
from models import (Opportunity, Application, Interview, Offer, User, notify,
                    APPLICATION_FLOW, APPLICATION_LABELS)
from security import log_action
from utils import roles_required, parse_skills

bp = Blueprint("employee", __name__, url_prefix="/employee")


@bp.before_request
@login_required
@roles_required("employee", "super_admin")
def guard():
    pass


WEIGHT = {"advanced": 1.0, "basic": 0.5, "none": 0.0}
LEVEL_ICON = {"advanced": "✅", "basic": "⚠️", "none": "❌"}
LEARNING = {
    "Cloud": "Cloud fundamentals → hands-on labs → an associate-level cloud certification",
    "Python": "Intermediate Python (OOP, testing) → build and ship a small automation project",
    "SQL": "Joins, window functions and query optimisation using a practice database",
    "React": "Component design, hooks and state management → build a dashboard app",
    "Java": "Core Java and collections → Spring Boot REST API project",
    "Excel": "Pivot tables, Power Query and financial modelling templates",
    "Communication": "Business writing and stakeholder-presentation workshop",
    "Sales Negotiation": "Consultative selling and negotiation role-plays with a mentor",
}


def _required(job):
    return [s.strip() for s in (job.required_skills or "").split(",") if s.strip()]


def match_percent(job, skills):
    req = _required(job)
    if not req:
        return 0
    return round(100 * sum(WEIGHT.get(skills.get(s, "none"), 0) for s in req) / len(req))


def _profile():
    return current_user.employee_profile


@bp.route("/")
def dashboard():
    emp = _profile()
    apps = Application.query.filter_by(applicant_id=current_user.id).order_by(Application.applied_at.desc()).all()
    upcoming = [iv for a in apps for iv in a.interviews if iv.result == "pending" and iv.scheduled_at and iv.scheduled_at >= utcnow()]
    upcoming.sort(key=lambda i: i.scheduled_at)
    return render_template("employee/dashboard.html", emp=emp, apps=apps[:4], upcoming=upcoming[:3],
                           flow=APPLICATION_FLOW, labels=APPLICATION_LABELS,
                           open_jobs=Opportunity.query.filter_by(status="open").count(),
                           pending_offers=sum(1 for a in apps if a.offer and a.offer.status == "pending"),
                           today=utcnow().date())


@bp.route("/profile", methods=["POST"])
def update_profile():
    emp = _profile()
    if not emp:
        flash("No employee profile to update for this account.", "warning")
        return redirect(url_for("employee.dashboard"))
    role = request.form.get("job_role", "").strip()[:120]
    if role:
        emp.job_role = role
    skills = parse_skills(request.form.get("skills", ""))
    emp.skills = ",".join(f"{k}:{v}" for k, v in skills.items())[:255]
    for field in ("job_satisfaction", "work_life_balance"):
        try:
            v = int(request.form.get(field, ""))
            if 1 <= v <= 4:
                setattr(emp, field, v)
        except ValueError:
            pass
    log_action("Updated own employee profile", "general")
    db.session.commit()
    flash("Profile updated.", "success")
    return redirect(url_for("employee.dashboard"))


@bp.route("/career")
def career():
    emp = _profile()
    skills = parse_skills(emp.skills if emp else "")
    jobs = Opportunity.query.filter_by(status="open").all()
    gap = Counter()
    ranked = []
    for j in jobs:
        for s in _required(j):
            if skills.get(s, "none") != "advanced":
                gap[s] += 1
        ranked.append((match_percent(j, skills), j))
    ranked.sort(key=lambda r: -r[0])
    rec = None
    if gap:
        skill, n = gap.most_common(1)[0]
        rec = {"skill": skill, "count": n, "path": LEARNING.get(skill, "Structured course plus a hands-on project"),
               "text": f"Based on your profile and available opportunities, improving your {skill} skills may "
                       f"increase your suitability for {n} internal opportunit{'y' if n == 1 else 'ies'}."}
    return render_template("employee/career.html", emp=emp, skills=skills, level_icon=LEVEL_ICON,
                           ranked=ranked, rec=rec, gaps=gap.most_common(4), learning=LEARNING)


@bp.route("/opportunities")
def opportunities():
    emp = _profile()
    skills = parse_skills(emp.skills if emp else "")
    jobs = Opportunity.query.filter_by(status="open").order_by(Opportunity.created_at.desc()).all()
    applied = {a.opportunity_id for a in Application.query.filter_by(applicant_id=current_user.id)}
    return render_template("employee/opportunities.html",
                           jobs=[(match_percent(j, skills), j) for j in jobs], applied=applied)


@bp.route("/opportunities/<int:opp_id>/apply", methods=["POST"])
def apply(opp_id):
    job = db.get_or_404(Opportunity, opp_id)
    if job.status != "open":
        flash("This opportunity is closed.", "warning")
    elif Application.query.filter_by(opportunity_id=opp_id, applicant_id=current_user.id).first():
        flash("You already applied to this opportunity.", "warning")
    else:
        db.session.add(Application(opportunity_id=opp_id, applicant_id=current_user.id))
        for u in User.query.filter(User.role.in_(["recruiter", "hr"]), User.is_active_flag.is_(True)).all():
            notify(u.id, f"{current_user.name} applied for '{job.title}'.", f"/recruitment/opportunities/{job.id}/candidates", "application")
        log_action(f"Applied for '{job.title}'", "hiring")
        db.session.commit()
        flash("Application submitted — you can track it under My Applications.", "success")
        return redirect(url_for("employee.applications"))
    return redirect(url_for("employee.opportunities"))


@bp.route("/applications")
def applications():
    apps = Application.query.filter_by(applicant_id=current_user.id).order_by(Application.applied_at.desc()).all()
    return render_template("employee/applications.html", apps=apps, flow=APPLICATION_FLOW, labels=APPLICATION_LABELS)


@bp.route("/interviews")
def interviews():
    items = (Interview.query.join(Application).filter(Application.applicant_id == current_user.id)
             .order_by(Interview.scheduled_at.desc()).all())
    return render_template("employee/interviews.html", items=items, now=utcnow())


@bp.route("/offers")
def offers():
    items = (Offer.query.join(Application).filter(Application.applicant_id == current_user.id)
             .order_by(Offer.created_at.desc()).all())
    return render_template("employee/offers.html", items=items)


@bp.route("/offers/<int:offer_id>/respond", methods=["POST"])
def respond_offer(offer_id):
    offer = db.get_or_404(Offer, offer_id)
    if offer.application.applicant_id != current_user.id or offer.status != "pending":
        flash("This offer cannot be changed.", "danger")
        return redirect(url_for("employee.offers"))
    accept = request.form.get("decision") == "accept"
    offer.status = "accepted" if accept else "declined"
    offer.application.status = "accepted" if accept else "declined"
    for u in User.query.filter(User.role.in_(["recruiter", "hr"]), User.is_active_flag.is_(True)).all():
        notify(u.id, f"{current_user.name} {'accepted' if accept else 'declined'} the offer for '{offer.application.opportunity.title}'.",
               "/recruitment/offers", "offer")
    log_action(f"Offer #{offer.id} {offer.status}", "hiring")
    db.session.commit()
    flash(f"Offer {offer.status}.", "success")
    return redirect(url_for("employee.offers"))
