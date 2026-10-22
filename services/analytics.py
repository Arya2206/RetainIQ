"""Dashboard analytics shared by the HR dashboard API and the CEO overview."""
from collections import defaultdict
from datetime import date
from flask import url_for
from models import (AttritionSnapshot, Department, Opportunity, RetentionAction)
from utils import (latest_map, visible_employees, replacement_cost, factors_of, parse_date)


def dept_level(high_pct):
    return "High" if high_pct >= 20 else ("Medium" if high_pct >= 10 else "Low")


def trend_series(dept_name=None, date_from=None, date_to=None, live_rows=None):
    q = AttritionSnapshot.query.order_by(AttritionSnapshot.month.asc())
    if dept_name:
        dep = Department.query.filter_by(name=dept_name).first()
        q = q.filter(AttritionSnapshot.department_id == (dep.id if dep else -1))
    months = defaultdict(lambda: {"hc": 0, "risk_w": 0.0, "high": 0, "exits": 0})
    for s in q.all():
        if date_from and s.month < date_from.replace(day=1):
            continue
        if date_to and s.month > date_to:
            continue
        m = months[s.month]
        m["hc"] += s.headcount or 0
        m["risk_w"] += (s.avg_risk or 0) * (s.headcount or 0)
        m["high"] += s.high_risk or 0
        m["exits"] += s.exits or 0
    labels, avg_risk, high, exits, rate = [], [], [], [], []
    for month in sorted(months):
        m = months[month]
        labels.append(month.strftime("%b %y"))
        avg_risk.append(round(m["risk_w"] / m["hc"], 1) if m["hc"] else 0)
        high.append(m["high"])
        exits.append(m["exits"])
        rate.append(round(100 * m["exits"] / m["hc"], 2) if m["hc"] else 0)
    today = date.today()
    if live_rows and (not date_to or date_to >= today.replace(day=1)):
        labels.append(today.strftime("%b %y") + " (live)")
        avg_risk.append(round(sum(p.risk_score for _, p in live_rows) / len(live_rows), 1))
        high.append(sum(1 for _, p in live_rows if p.risk_level == "High"))
        exits.append(None)
        rate.append(None)
    return {"labels": labels, "avg_risk": avg_risk, "high_risk": high, "exits": exits, "exit_rate": rate}


def dashboard_data(user, dept=None, risk=None, date_from=None, date_to=None, detail_endpoint="hr.employee_detail"):
    emps = visible_employees(user)
    if dept:
        emps = [e for e in emps if e.department and e.department.name == dept]
    pm = latest_map(as_of=date_to, employee_ids=[e.id for e in emps])
    rows = [(e, pm[e.id]) for e in emps if e.id in pm]

    levels = {"High": 0, "Medium": 0, "Low": 0}
    for _, p in rows:
        levels[p.risk_level] = levels.get(p.risk_level, 0) + 1
    n = len(rows)

    by_dept = defaultdict(lambda: {"scores": [], "high": 0, "hc": 0})
    for e in emps:
        by_dept[e.department.name if e.department else "Unassigned"]["hc"] += 1
    for e, p in rows:
        d = by_dept[e.department.name if e.department else "Unassigned"]
        d["scores"].append(p.risk_score)
        d["high"] += p.risk_level == "High"
    dept_risk = []
    for name, d in by_dept.items():
        avg = round(sum(d["scores"]) / len(d["scores"]), 1) if d["scores"] else 0
        hp = round(100 * d["high"] / len(d["scores"]), 1) if d["scores"] else 0
        dept_risk.append({"name": name, "avg": avg, "high": d["high"], "headcount": d["hc"],
                          "high_pct": hp, "level": dept_level(hp)})
    dept_risk.sort(key=lambda x: (-x["high_pct"], -x["avg"]))

    listed = [(e, p) for e, p in rows if not risk or p.risk_level == risk]
    listed.sort(key=lambda r: -r[1].risk_score)
    top = []
    for e, p in listed[:8]:
        f = factors_of(p)
        top.append({"id": e.id, "name": e.name, "dept": e.department.name if e.department else "-",
                    "role": e.job_role, "score": p.risk_score, "level": p.risk_level,
                    "factor": f[0] if f else "-", "url": url_for(detail_endpoint, emp_id=e.id)})

    open_q = Opportunity.query.filter_by(status="open")
    if dept:
        dep = Department.query.filter_by(name=dept).first()
        open_q = open_q.filter_by(department_id=dep.id if dep else -1)

    ids = [e.id for e in emps]
    acts = {"planned": 0, "in_progress": 0, "done": 0}
    if ids:
        for a in RetentionAction.query.filter(RetentionAction.employee_id.in_(ids)).all():
            acts[a.status] = acts.get(a.status, 0) + 1

    exp_cost = sum(p.probability * replacement_cost(e) for e, p in rows)
    high_payroll = sum(e.annual_salary for e, p in rows if p.risk_level == "High")

    return {
        "kpis": {"employees": len(emps), "analyzed": n, "high": levels["High"], "medium": levels["Medium"],
                 "low": levels["Low"], "avg_risk": round(sum(p.risk_score for _, p in rows) / n, 1) if n else 0,
                 "open_jobs": open_q.count(), "expected_cost": round(exp_cost),
                 "payroll_at_risk": round(high_payroll)},
        "distribution": {"labels": ["Low", "Medium", "High"],
                         "counts": [levels["Low"], levels["Medium"], levels["High"]],
                         "pct": [round(100 * levels[k] / n) if n else 0 for k in ("Low", "Medium", "High")]},
        "dept_risk": dept_risk,
        "trend": trend_series(dept, date_from, date_to, rows),
        "top": top, "actions": acts,
    }
