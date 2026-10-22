from flask import Blueprint, request, redirect, url_for, flash
from flask_login import login_required, current_user
from extensions import db
from models import Employee, RetentionAction
from security import permission_required, log_action
from utils import parse_date, can_view_employee
from flask import abort

bp = Blueprint("retention", __name__, url_prefix="/retention")

HOME = {"hr": "hr.employee_detail", "super_admin": "hr.employee_detail", "manager": "manager.employee_detail",
        "tech_head": "techhead.employee_detail", "ceo": "ceo.employee_detail"}


def _back(emp_id):
    return redirect(url_for(HOME.get(current_user.role, "auth.dashboard"), emp_id=emp_id))


@bp.route("/<int:emp_id>/add", methods=["POST"])
@login_required
@permission_required("manage_retention_actions")
def add(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    if not can_view_employee(current_user, emp):
        abort(403)
    text = request.form.get("action", "").strip()[:255]
    if not text:
        flash("Please choose or type an action.", "danger")
        return _back(emp_id)
    db.session.add(RetentionAction(employee_id=emp.id, action=text, owner_id=current_user.id,
                                   due_date=parse_date(request.form.get("due_date"))))
    log_action(f"Added retention action for {emp.name}: {text}", "ai")
    db.session.commit()
    flash("Added to the retention plan.", "success")
    return _back(emp_id)


@bp.route("/action/<int:aid>/status", methods=["POST"])
@login_required
@permission_required("manage_retention_actions")
def status(aid):
    a = db.get_or_404(RetentionAction, aid)
    if not can_view_employee(current_user, a.employee):
        abort(403)
    s = request.form.get("status")
    if s in ("planned", "in_progress", "done"):
        a.status = s
        log_action(f"Retention action #{a.id} → {s}", "ai")
        db.session.commit()
    return _back(a.employee_id)
