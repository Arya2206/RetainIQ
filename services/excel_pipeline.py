"""Excel -> AI pipeline.
Upload -> Validate -> Clean -> Analyse -> Predict -> Classify -> Recommend -> Dashboard.
Every stage records counts so the UI (and the report) can show exactly what happened."""
import json
import os
import re
import secrets
import pandas as pd
from flask import current_app

from extensions import db
from models import (User, Employee, Department, AttritionPrediction, UploadBatch, notify)
from ml.predictor import predict_many, drift_report
from security import log_action

REQUIRED = ["Name", "Email", "Department", "JobRole", "Age", "MonthlySalary", "YearsAtCompany",
            "JobSatisfaction", "WorkLifeBalance", "OverTime", "PerformanceRating",
            "YearsSincePromotion", "TrainingTimesLastYear", "DistanceFromHome",
            "NumCompaniesWorked", "SalaryHikePercent"]
NUMERIC = ["Age", "MonthlySalary", "YearsAtCompany", "JobSatisfaction", "WorkLifeBalance",
           "PerformanceRating", "YearsSincePromotion", "TrainingTimesLastYear",
           "DistanceFromHome", "NumCompaniesWorked", "SalaryHikePercent"]
RANGES = {"Age": (16, 80), "MonthlySalary": (1000, 5_000_000), "YearsAtCompany": (0, 50),
          "JobSatisfaction": (1, 4), "WorkLifeBalance": (1, 4), "PerformanceRating": (1, 5),
          "YearsSincePromotion": (0, 40), "TrainingTimesLastYear": (0, 52),
          "DistanceFromHome": (0, 500), "NumCompaniesWorked": (0, 30), "SalaryHikePercent": (0, 100)}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PipelineError(Exception):
    pass


def _norm(c):
    return re.sub(r"[\s_\-]", "", str(c)).lower()


def run_pipeline(path, original_name, user):
    report = {"stages": [], "missing_filled": {}, "clipped": {}, "rejected": [], "duplicates": 0,
              "inconsistent": {}, "drift": {}}

    # 1 ---- Upload / read
    try:
        df = pd.read_excel(path, engine="openpyxl")
    except Exception:
        raise PipelineError("The file could not be read. Please upload a valid .xlsx workbook.")
    if df.empty:
        raise PipelineError("The workbook has no data rows.")
    max_rows = current_app.config["MAX_UPLOAD_ROWS"]
    if len(df) > max_rows:
        raise PipelineError(f"The file has {len(df)} rows; the limit is {max_rows} per upload.")
    total = len(df)
    report["stages"].append({"name": "Upload", "detail": f"{total} rows read from {original_name}", "ok": True})

    # 2 ---- Validate structure
    canon = {_norm(c): c for c in REQUIRED}
    df = df.rename(columns={c: canon[_norm(c)] for c in df.columns if _norm(c) in canon})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise PipelineError("Missing required column(s): " + ", ".join(missing))
    df = df[REQUIRED].copy()
    df.insert(0, "_row", range(2, len(df) + 2))   # Excel row numbers (header = row 1)

    for c in ("Name", "Email", "Department", "JobRole"):
        df[c] = df[c].astype("string").str.strip()
    df["Email"] = df["Email"].str.lower()

    keep = []
    for idx, r in df.iterrows():
        reason = None
        if pd.isna(r["Name"]) or not str(r["Name"]).strip():
            reason = "Name is empty"
        elif pd.isna(r["Email"]) or not EMAIL_RE.match(str(r["Email"])):
            reason = "Invalid email address"
        if reason:
            report["rejected"].append({"row": int(r["_row"]), "reason": reason})
        else:
            keep.append(idx)
    df = df.loc[keep]
    before = len(df)
    df = df.drop_duplicates(subset="Email", keep="last")
    report["duplicates"] = before - len(df)
    report["stages"].append({
        "name": "Validate",
        "detail": f"{len(df)} valid, {len(report['rejected'])} rejected, {report['duplicates']} duplicate email(s) merged",
        "ok": True})

    # 3 ---- Clean: coerce, impute, clip, normalise
    for c in NUMERIC:
        coerced = pd.to_numeric(df[c], errors="coerce")
        n_missing = int(coerced.isna().sum())
        if n_missing:
            grp = coerced.groupby(df["Department"]).transform("median")
            coerced = coerced.fillna(grp).fillna(coerced.median()).fillna(RANGES[c][0])
            report["missing_filled"][c] = n_missing
        lo, hi = RANGES[c]
        clipped = int(((coerced < lo) | (coerced > hi)).sum())
        if clipped:
            report["clipped"][c] = clipped
        df[c] = coerced.clip(lo, hi)
    # Cross-field sanity checks: individually valid numbers can still contradict each other.
    n_promo = int((df["YearsSincePromotion"] > df["YearsAtCompany"]).sum())
    if n_promo:                                   # cannot be un-promoted for longer than you have worked here
        df["YearsSincePromotion"] = df[["YearsSincePromotion", "YearsAtCompany"]].min(axis=1)
        report["inconsistent"]["YearsSincePromotion > YearsAtCompany (capped to tenure)"] = n_promo
    n_tenure = int((df["YearsAtCompany"] > df["Age"] - 15).sum())
    if n_tenure:                                  # joined before a plausible working age
        df["YearsAtCompany"] = df[["YearsAtCompany"]].assign(cap=(df["Age"] - 15).clip(lower=0)).min(axis=1)
        df["YearsSincePromotion"] = df[["YearsSincePromotion", "YearsAtCompany"]].min(axis=1)
        report["inconsistent"]["YearsAtCompany implies joining before age 15 (capped)"] = n_tenure
    df["OverTime"] = df["OverTime"].astype("string").str.strip().str.lower().isin(["yes", "y", "true", "1", "1.0"])
    df["Department"] = df["Department"].fillna("Unassigned").replace("", "Unassigned")
    df["JobRole"] = df["JobRole"].fillna("Not set")
    n_fix = (sum(report["missing_filled"].values()) + sum(report["clipped"].values())
             + sum(report["inconsistent"].values()))
    report["stages"].append({"name": "Clean", "detail": f"{n_fix} value(s) imputed, clipped or reconciled", "ok": True})

    # 4 ---- Persist employees (lookups are preloaded once instead of one query per row)
    created = updated = 0
    emp_objs = []
    dept_cache = {d.name: d for d in Department.query.all()}
    emails = [str(e) for e in df["Email"]]
    user_cache = {u.email: u for u in User.query.filter(User.email.in_(emails)).all()}
    emp_cache = {e.user_id: e for e in Employee.query.filter(Employee.user_id.in_([u.id for u in user_cache.values()] or [-1])).all()}
    for _, r in df.iterrows():
        dname = str(r["Department"])[:80]
        dept = dept_cache.get(dname)
        if not dept:
            dept = Department(name=dname)
            db.session.add(dept)
            db.session.flush()
            dept_cache[dname] = dept
        u = user_cache.get(str(r["Email"]))
        if u and u.role != "employee":
            report["rejected"].append({"row": int(r["_row"]), "reason": "Email belongs to a staff account"})
            continue
        if not u:
            u = User(name=str(r["Name"])[:120], email=str(r["Email"]), role="employee",
                     department_id=dept.id, must_change_password=True)
            # Bulk-imported accounts get an unusable password marker (hashing 1000s of
            # passwords is deliberately slow); an administrator issues a real temporary
            # password via "Reset password" when the person first needs to sign in.
            u.password_hash = "!unusable$" + secrets.token_hex(16)
            db.session.add(u)
            db.session.flush()
            user_cache[u.email] = u
            created += 1
        else:
            updated += 1
        emp = emp_cache.get(u.id)
        if not emp:
            emp = Employee(user_id=u.id, employee_code=f"EMP{u.id:04d}")
            db.session.add(emp)
            emp_cache[u.id] = emp
        u.department_id = dept.id
        emp.department_id = dept.id
        emp.job_role = str(r["JobRole"])[:120]
        emp.age = int(r["Age"]); emp.monthly_salary = float(r["MonthlySalary"])
        emp.years_at_company = float(r["YearsAtCompany"]); emp.job_satisfaction = int(r["JobSatisfaction"])
        emp.work_life_balance = int(r["WorkLifeBalance"]); emp.overtime = bool(r["OverTime"])
        emp.performance_rating = int(r["PerformanceRating"])
        emp.years_since_promotion = float(r["YearsSincePromotion"])
        emp.training_times_last_year = int(r["TrainingTimesLastYear"])
        emp.distance_from_home = float(r["DistanceFromHome"])
        emp.num_companies_worked = int(r["NumCompaniesWorked"])
        emp.salary_hike_percent = float(r["SalaryHikePercent"])
        emp_objs.append(emp)
    db.session.flush()
    report["stages"].append({"name": "Analyse", "detail": f"{len(emp_objs)} employee profiles built ({created} new, {updated} updated)", "ok": True})

    # 5 ---- Predict (vectorised) + classify + recommend
    batch = UploadBatch(filename=original_name[:255], uploaded_by=user.id, rows_total=total,
                        rows_valid=len(emp_objs), rows_rejected=len(report["rejected"]),
                        employees_created=created, employees_updated=updated)
    db.session.add(batch)
    db.session.flush()
    results = predict_many([e.to_feature_dict() for e in emp_objs])
    counts = {"High": 0, "Medium": 0, "Low": 0}
    scored = []
    for emp, res in zip(emp_objs, results):
        db.session.add(AttritionPrediction(
            employee_id=emp.id, probability=res["probability"], risk_score=res["risk_score"],
            risk_level=res["risk_level"], top_factors=json.dumps(res["top_factors"]),
            recommendations=json.dumps(res["recommendations"]),
            explanations=json.dumps(res["explanations"]), source="excel_upload", batch_id=batch.id))
        counts[res["risk_level"]] += 1
        scored.append((res["risk_score"], emp.id))
    report["drift"] = drift_report([e.to_feature_dict() for e in emp_objs])
    report["stages"].append({"name": "Predict", "detail": f"{len(results)} calibrated attrition probabilities computed by the ML model", "ok": True})
    report["stages"].append({"name": "Classify", "detail": f"{counts['High']} High · {counts['Medium']} Medium · {counts['Low']} Low", "ok": True})
    report["stages"].append({"name": "Recommend", "detail": "Risk factors and retention actions generated for every employee", "ok": True})
    report["risk_counts"] = counts
    report["top_ids"] = [i for _, i in sorted(scored, reverse=True)[:10]]
    batch.high_risk, batch.medium_risk, batch.low_risk = counts["High"], counts["Medium"], counts["Low"]
    batch.rows_rejected = len(report["rejected"])
    report["rejected"] = report["rejected"][:100]
    batch.report_json = json.dumps(report)

    # 6 ---- Notify + audit
    for hr in User.query.filter(User.role.in_(["hr", "super_admin"]), User.is_active_flag.is_(True)).all():
        notify(hr.id, f"{len(emp_objs)} employees analysed from {original_name}; {counts['High']} high-risk detected.",
               f"/hr/upload/{batch.id}", "risk")
    log_action(f"Excel pipeline: {len(emp_objs)} analysed, {counts['High']} high-risk ({original_name})", "data", user)
    db.session.commit()
    return batch


def sample_workbook_bytes(n=25):
    """A ready-to-use template with realistic rows (including a few deliberate data-quality issues)."""
    import io, random
    rng = random.Random(11)
    depts = ["IT", "HR", "Sales", "Finance", "Marketing", "Operations"]
    roles = {"IT": "Software Developer", "HR": "HR Executive", "Sales": "Sales Executive",
             "Finance": "Financial Analyst", "Marketing": "Marketing Executive", "Operations": "Process Analyst"}
    first = ["Rahul", "Priya", "Amit", "Sneha", "Vikram", "Anjali", "Rohan", "Kavya", "Arjun", "Neha", "Karan", "Divya"]
    last = ["Sharma", "Verma", "Patel", "Gupta", "Singh", "Iyer", "Nair", "Reddy", "Mehta", "Joshi"]
    rows = []
    for i in range(n):
        d = rng.choice(depts)
        nm = f"{rng.choice(first)} {rng.choice(last)}"
        rows.append({
            "Name": nm, "Email": f"{nm.split()[0].lower()}.{nm.split()[1].lower()}{i}@sample.example",
            "Department": d, "JobRole": roles[d], "Age": rng.randint(22, 55),
            "MonthlySalary": rng.randint(25000, 150000), "YearsAtCompany": round(rng.uniform(0.2, 15), 1),
            "JobSatisfaction": rng.randint(1, 4), "WorkLifeBalance": rng.randint(1, 4),
            "OverTime": rng.choice(["Yes", "No"]), "PerformanceRating": rng.randint(1, 5),
            "YearsSincePromotion": round(rng.uniform(0, 6), 1), "TrainingTimesLastYear": rng.randint(0, 6),
            "DistanceFromHome": round(rng.uniform(1, 45), 1), "NumCompaniesWorked": rng.randint(0, 6),
            "SalaryHikePercent": round(rng.uniform(3, 25), 1)})
    df = pd.DataFrame(rows)
    df.loc[2, "JobSatisfaction"] = None            # missing value  -> imputed
    df.loc[5, "MonthlySalary"] = None
    df.loc[7, "Email"] = "not-an-email"            # invalid email  -> rejected
    df.loc[9, "WorkLifeBalance"] = 9               # out of range   -> clipped
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return buf
