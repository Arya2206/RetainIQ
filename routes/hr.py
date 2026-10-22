import csv, io, json, os
from datetime import date
from flask import (Blueprint, render_template, request, redirect, url_for, flash, current_app,
                   send_file, Response, abort)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db, utcnow
from models import (Employee, Department, AttritionPrediction, UploadBatch, RetentionAction, User, notify)
from ml.predictor import predict_employee, model_info
from ml.train_model import FEATURES
from security import permission_required, log_action
from services import excel_pipeline
from utils import (roles_required, risk_badge, latest_map, latest_prediction, factors_of, recs_of,
                   explanations_of, parse_date, visible_employees, replacement_cost)

bp = Blueprint("hr", __name__, url_prefix="/hr")


@bp.before_request
@login_required
@roles_required("hr", "super_admin")
def guard():
    pass


def _save_prediction(emp, source="manual"):
    res = predict_employee(emp.to_feature_dict())
    pred = AttritionPrediction(
        employee_id=emp.id, probability=res["probability"], risk_score=res["risk_score"],
        risk_level=res["risk_level"], top_factors=json.dumps(res["top_factors"]),
        recommendations=json.dumps(res["recommendations"]), explanations=json.dumps(res["explanations"]),
        source=source)
    db.session.add(pred)
    return pred, res


@bp.route("/")
@permission_required("view_attrition_dashboard")
def dashboard():
    info = model_info()["metrics"]
    last_batch = UploadBatch.query.order_by(UploadBatch.created_at.desc()).first()
    return render_template("hr/dashboard.html", departments=Department.query.order_by(Department.name).all(),
                           metrics=info, last_batch=last_batch, today=date.today().isoformat())


@bp.route("/employees")
@permission_required("view_attrition_dashboard")
def employees():
    q = request.args.get("q", "").strip().lower()
    dept = request.args.get("department", "")
    risk = request.args.get("risk", "")
    sort = request.args.get("sort", "risk")
    page = max(1, request.args.get("page", 1, type=int))
    emps = Employee.query.all()
    pm = latest_map()
    rows = []
    for e in emps:
        p = pm.get(e.id)
        if dept and (not e.department or e.department.name != dept):
            continue
        if risk and (not p or p.risk_level != risk):
            continue
        if q and q not in (e.name or "").lower() and q not in (e.job_role or "").lower() and q not in (e.employee_code or "").lower():
            continue
        rows.append((e, p))
    keys = {"risk": lambda r: -(r[1].risk_score if r[1] else -1), "name": lambda r: (r[0].name or "").lower(),
            "salary": lambda r: -(r[0].monthly_salary or 0), "dept": lambda r: (r[0].department.name if r[0].department else "")}
    rows.sort(key=keys.get(sort, keys["risk"]))
    per = 15
    total = len(rows)
    pages = max(1, -(-total // per))
    page = min(page, pages)
    return render_template("hr/employees.html", rows=rows[(page - 1) * per: page * per], total=total, page=page,
                           pages=pages, departments=Department.query.order_by(Department.name).all(),
                           dept_filter=dept, risk_filter=risk, q=q, sort=sort, risk_badge=risk_badge)


@bp.route("/employees/<int:emp_id>")
@permission_required("view_attrition_dashboard")
def employee_detail(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    p = latest_prediction(emp.id)
    history = (AttritionPrediction.query.filter_by(employee_id=emp.id)
               .order_by(AttritionPrediction.created_at.asc(), AttritionPrediction.id.asc()).all())
    feats = emp.to_feature_dict()
    sliders = [
        ("JobSatisfaction", "Job satisfaction", 1, 4, 1, feats["JobSatisfaction"]),
        ("WorkLifeBalance", "Work-life balance", 1, 4, 1, feats["WorkLifeBalance"]),
        ("SalaryHikePercent", "Salary hike (%)", 0, 30, 1, feats["SalaryHikePercent"]),
        ("YearsSincePromotion", "Years since promotion", 0, 10, 0.5, feats["YearsSincePromotion"]),
        ("TrainingTimesLastYear", "Training sessions / year", 0, 10, 1, feats["TrainingTimesLastYear"]),
        ("DistanceFromHome", "Commute distance (km)", 0, 60, 1, feats["DistanceFromHome"]),
    ]
    return render_template(
        "hr/employee_detail.html", emp=emp, p=p, history=history[::-1], history_json=[
            {"d": h.created_at.strftime("%d %b %y"), "s": h.risk_score} for h in history],
        factors=factors_of(p), recs=recs_of(p), expl=explanations_of(p), sliders=sliders,
        overtime=feats["OverTime"], actions=RetentionAction.query.filter_by(employee_id=emp.id)
        .order_by(RetentionAction.created_at.desc()).all(), risk_badge=risk_badge,
        replacement=replacement_cost(emp))


@bp.route("/employees/<int:emp_id>/predict", methods=["POST"])
@permission_required("run_predictions")
def run_prediction(emp_id):
    emp = db.get_or_404(Employee, emp_id)
    pred, res = _save_prediction(emp)
    if res["risk_level"] == "High":
        mgr_id = emp.user.manager_id if emp.user else None
        notify(mgr_id, f"High attrition risk flagged for {emp.name} ({res['risk_score']}/100).",
               f"/manager/employee/{emp.id}", "risk")
    log_action(f"Ran AI prediction for {emp.name}: {res['risk_level']} ({res['risk_score']})", "ai")
    db.session.commit()
    flash(f"Prediction updated: {emp.name} is {res['risk_level']} risk ({res['risk_score']}/100).", "success")
    return redirect(url_for("hr.employee_detail", emp_id=emp.id))


# ---------------------------------------------------------------- Excel pipeline
@bp.route("/upload", methods=["GET", "POST"])
@permission_required("upload_excel")
def upload():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Please choose an Excel file (.xlsx).", "danger")
            return redirect(url_for("hr.upload"))
        if not f.filename.lower().endswith(".xlsx"):
            flash("Only .xlsx workbooks are supported.", "danger")
            return redirect(url_for("hr.upload"))
        os.makedirs(current_app.config["UPLOAD_FOLDER"], exist_ok=True)
        path = os.path.join(current_app.config["UPLOAD_FOLDER"],
                            secure_filename(f"{int(utcnow().timestamp())}_{f.filename}"))
        f.save(path)
        try:
            batch = excel_pipeline.run_pipeline(path, secure_filename(f.filename), current_user)
        except excel_pipeline.PipelineError as e:
            db.session.rollback()
            flash(str(e), "danger")
            return redirect(url_for("hr.upload"))
        finally:
            if os.path.exists(path):
                os.remove(path)
        return redirect(url_for("hr.upload_result", batch_id=batch.id))
    return render_template("hr/upload.html", required=excel_pipeline.REQUIRED,
                           batches=UploadBatch.query.order_by(UploadBatch.created_at.desc()).limit(8).all())


@bp.route("/upload/<int:batch_id>")
@permission_required("view_attrition_dashboard")
def upload_result(batch_id):
    b = db.get_or_404(UploadBatch, batch_id)
    rep = json.loads(b.report_json or "{}")
    top = [(e, latest_prediction(e.id)) for e in
           [db.session.get(Employee, i) for i in rep.get("top_ids", [])] if e]
    return render_template("hr/upload_result.html", b=b, rep=rep, top=top, risk_badge=risk_badge)


@bp.route("/upload/template.xlsx")
@permission_required("upload_excel")
def upload_template():
    return send_file(excel_pipeline.sample_workbook_bytes(), as_attachment=True,
                     download_name="retainiq_employee_data_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- export & model
@bp.route("/export/risk.csv")
@permission_required("export_reports")
def export_risk():
    dept, risk = request.args.get("department", ""), request.args.get("risk", "")
    pm = latest_map()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Employee", "Code", "Department", "Role", "MonthlySalary", "RiskScore", "RiskLevel",
                "TopFactors", "SuggestedActions", "LastPredicted"])
    n = 0
    for e in visible_employees(current_user):
        p = pm.get(e.id)
        if not p or (dept and (not e.department or e.department.name != dept)) or (risk and p.risk_level != risk):
            continue
        w.writerow([e.name, e.employee_code, e.department.name if e.department else "", e.job_role,
                    e.monthly_salary, p.risk_score, p.risk_level, "; ".join(factors_of(p)),
                    "; ".join(recs_of(p)), p.created_at.strftime("%Y-%m-%d")])
        n += 1
    log_action(f"Exported risk report ({n} rows)", "data")
    db.session.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=retainiq_attrition_risk_report.csv"})


@bp.route("/model")
@permission_required("view_attrition_dashboard")
def model_insights():
    info = model_info()
    labels = info["feature_labels"]
    imp = sorted(info["feature_importances"].items(), key=lambda kv: -kv[1])
    return render_template("hr/model_insights.html", m=info["metrics"],
                           importances=[(labels[k], v) for k, v in imp])
