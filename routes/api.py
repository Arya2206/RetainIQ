from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from extensions import db
from models import Employee
from ml.predictor import model_info, what_if, _load
from security import permission_required, has_permission
from services.analytics import dashboard_data
from services.assistant import answer
from utils import (parse_date, latest_prediction, factors_of, recs_of, explanations_of, can_view_employee)

bp = Blueprint("api", __name__, url_prefix="/api/v1")


@bp.route("/health")
def health():
    ok_model = True
    try:
        _load()
    except Exception:
        ok_model = False
    try:
        db.session.execute(db.text("SELECT 1"))
        ok_db = True
    except Exception:
        ok_db = False
    return jsonify(status="ok" if ok_model and ok_db else "degraded", model_loaded=ok_model,
                   database=ok_db, version=current_app.config["APP_VERSION"])


@bp.route("/hr/dashboard")
@login_required
@permission_required("view_attrition_dashboard")
def hr_dashboard():
    return jsonify(dashboard_data(current_user, dept=request.args.get("dept") or None,
                                  risk=request.args.get("risk") or None,
                                  date_from=parse_date(request.args.get("from")),
                                  date_to=parse_date(request.args.get("to"))))


def _risk_viewer():
    return any(has_permission(current_user, p) for p in ("view_attrition_dashboard", "view_team", "view_executive_dashboard"))


@bp.route("/employees/<int:emp_id>/risk")
@login_required
def employee_risk(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    if not _risk_viewer() or not can_view_employee(current_user, emp):
        return jsonify(error="forbidden"), 403
    p = latest_prediction(emp.id)
    if not p:
        return jsonify(error="no prediction yet"), 404
    return jsonify(employee=emp.name, risk_score=p.risk_score, risk_level=p.risk_level,
                   probability=p.probability, factors=factors_of(p), actions=recs_of(p),
                   explanations=explanations_of(p), predicted_at=p.created_at.isoformat())


@bp.route("/predict/what-if", methods=["POST"])
@login_required
@permission_required("what_if_simulation")
def api_what_if():
    data = request.get_json(silent=True) or {}
    emp = db.session.get(Employee, int(data.get("employee_id") or 0))
    if not emp or not can_view_employee(current_user, emp):
        return jsonify(error="employee not found"), 404
    allowed = {"JobSatisfaction": (1, 4), "WorkLifeBalance": (1, 4), "SalaryHikePercent": (0, 30),
               "YearsSincePromotion": (0, 10), "TrainingTimesLastYear": (0, 10), "DistanceFromHome": (0, 60),
               "OverTime": (0, 1), "MonthlySalary": (1000, 5_000_000)}
    changes = {}
    for k, v in (data.get("changes") or {}).items():
        if k in allowed:
            try:
                lo, hi = allowed[k]
                changes[k] = min(max(float(v), lo), hi)
            except (TypeError, ValueError):
                pass
    r = what_if(emp.to_feature_dict(), changes)
    return jsonify(current=r["current"]["risk_score"], simulated=r["simulated"]["risk_score"],
                   delta=r["delta"], level=r["simulated"]["risk_level"],
                   factors=r["simulated"]["top_factors"], actions=r["simulated"]["recommendations"])


@bp.route("/model/info")
@login_required
def api_model_info():
    if not _risk_viewer():
        return jsonify(error="forbidden"), 403
    m = model_info()
    return jsonify(metrics={k: v for k, v in m["metrics"].items() if k != "roc_curve"},
                   feature_importances=m["feature_importances"])


@bp.route("/assistant", methods=["POST"])
@login_required
@permission_required("use_ai_assistant")
def assistant():
    return jsonify(answer((request.get_json(silent=True) or {}).get("question", "")[:300], current_user))
