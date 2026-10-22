from collections import defaultdict
from flask import Blueprint, render_template, abort, url_for
from flask_login import login_required, current_user
from extensions import db
from models import Employee, Opportunity, Department, RetentionAction
from security import permission_required
from utils import (roles_required, risk_badge, latest_map, visible_employees, can_view_employee,
                   latest_prediction, factors_of, recs_of, explanations_of, parse_skills)

bp = Blueprint("techhead", __name__, url_prefix="/tech")


@bp.before_request
@login_required
@roles_required("tech_head", "super_admin")
def guard():
    pass


@bp.route("/")
@permission_required("view_team")
def dashboard():
    dept = current_user.department
    emps = visible_employees(current_user)
    pm = latest_map(employee_ids=[e.id for e in emps])
    members = sorted([(e, pm.get(e.id)) for e in emps], key=lambda m: -(m[1].risk_score if m[1] else -1))
    n = len(emps)
    skills = defaultdict(lambda: {"advanced": 0, "basic": 0, "none": 0})
    for e in emps:
        for s, lvl in parse_skills(e.skills).items():
            skills[s][lvl] += 1
    jobs = Opportunity.query.filter_by(status="open")
    if dept:
        jobs = jobs.filter_by(department_id=dept.id)
    lv = [sum(1 for _, p in members if p and p.risk_level == k) for k in ("Low", "Medium", "High")]
    return render_template(
        "techhead/dashboard.html", dept=dept, members=members, team_size=n, high=lv[2], levels=lv,
        avg_perf=round(sum(e.performance_rating or 0 for e in emps) / n, 2) if n else 0,
        overtime_pct=round(100 * sum(1 for e in emps if e.overtime) / n) if n else 0,
        avg_sat=round(sum(e.job_satisfaction or 0 for e in emps) / n, 2) if n else 0,
        skills=sorted(skills.items(), key=lambda kv: -(kv[1]["advanced"] + kv[1]["basic"])),
        open_jobs=jobs.all(), risk_badge=risk_badge)


@bp.route("/employee/<int:emp_id>")
@permission_required("view_team")
def employee_detail(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    if not can_view_employee(current_user, emp):
        abort(403)
    p = latest_prediction(emp.id)
    return render_template("manager/employee_detail.html", emp=emp, p=p, factors=factors_of(p), recs=recs_of(p),
                           expl=explanations_of(p), risk_badge=risk_badge, back_url=url_for("techhead.dashboard"),
                           back_label="tech dashboard",
                           actions=RetentionAction.query.filter_by(employee_id=emp.id).order_by(RetentionAction.created_at.desc()).all())
