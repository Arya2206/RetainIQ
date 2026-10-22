from collections import Counter
from flask import Blueprint, render_template, abort, url_for
from flask_login import login_required, current_user
from extensions import db
from models import Employee, RetentionAction
from security import permission_required
from services.analytics import dashboard_data
from utils import (roles_required, risk_badge, latest_map, factors_of, recs_of, explanations_of,
                   latest_prediction)

bp = Blueprint("ceo", __name__, url_prefix="/ceo")


@bp.before_request
@login_required
@roles_required("ceo", "super_admin")
def guard():
    pass


@bp.route("/")
@permission_required("view_executive_dashboard")
def dashboard():
    data = dashboard_data(current_user, detail_endpoint="ceo.employee_detail")
    drivers = Counter()
    for p in latest_map().values():
        if p.risk_level in ("High", "Medium"):
            drivers.update(factors_of(p))
    total_payroll = sum(e.annual_salary for e in Employee.query.all())
    return render_template("ceo/dashboard.html", d=data, drivers=drivers.most_common(6), total_payroll=total_payroll)


@bp.route("/employee/<int:emp_id>")
@permission_required("view_executive_dashboard")
def employee_detail(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    p = latest_prediction(emp.id)
    return render_template("manager/employee_detail.html", emp=emp, p=p, factors=factors_of(p), recs=recs_of(p),
                           expl=explanations_of(p), risk_badge=risk_badge, back_url=url_for("ceo.dashboard"),
                           back_label="executive overview",
                           actions=RetentionAction.query.filter_by(employee_id=emp.id).all())
