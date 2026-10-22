from flask import Blueprint, render_template, abort, url_for
from flask_login import login_required, current_user
from models import RetentionAction
from security import permission_required
from utils import (roles_required, risk_badge, latest_map, visible_employees, can_view_employee,
                   factors_of, recs_of, explanations_of, latest_prediction)
from extensions import db
from models import Employee

bp = Blueprint("manager", __name__, url_prefix="/manager")


@bp.before_request
@login_required
@roles_required("manager", "super_admin")
def guard():
    pass


@bp.route("/")
@permission_required("view_team")
def dashboard():
    team = visible_employees(current_user)
    pm = latest_map(employee_ids=[e.id for e in team])
    rows = sorted([(e, pm.get(e.id)) for e in team], key=lambda r: -(r[1].risk_score if r[1] else -1))
    high = [r for r in rows if r[1] and r[1].risk_level == "High"]
    perf = [e.performance_rating or 0 for e in team]
    dist = [sum(1 for x in perf if x == k) for k in range(1, 6)]
    open_actions = RetentionAction.query.filter(RetentionAction.employee_id.in_([e.id for e in team]),
                                                RetentionAction.status != "done").count() if team else 0
    return render_template("manager/dashboard.html", rows=rows, high=high,
                           avg_perf=round(sum(perf) / len(perf), 2) if perf else 0, perf_dist=dist,
                           open_actions=open_actions, risk_badge=risk_badge)


@bp.route("/employee/<int:emp_id>")
@permission_required("view_team")
def employee_detail(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    if not can_view_employee(current_user, emp):
        abort(403)
    p = latest_prediction(emp.id)
    return render_template("manager/employee_detail.html", emp=emp, p=p, factors=factors_of(p), recs=recs_of(p),
                           expl=explanations_of(p), risk_badge=risk_badge, back_url=url_for("manager.dashboard"),
                           back_label="my team",
                           actions=RetentionAction.query.filter_by(employee_id=emp.id).order_by(RetentionAction.created_at.desc()).all())
