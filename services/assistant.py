"""AI HR Assistant: a deterministic intent engine that answers natural-language
questions by querying the live database. Every answer is scoped to what the
asking user is allowed to see (managers see only their team, etc.)."""
import re
from collections import Counter, defaultdict
from flask import url_for
from models import Department, Opportunity, Offer, Application, Interview, RetentionAction, AttritionSnapshot
from utils import latest_map, visible_employees, factors_of

SUGGESTIONS = [
    "Which department has the highest attrition risk?",
    "Show employees with high attrition risk and low job satisfaction",
    "Who works overtime and is high risk?",
    "What are the top reasons employees may leave?",
    "How many high risk employees are there?",
    "Show the attrition trend",
]


def _item(e, p):
    return {"name": e.name, "dept": e.department.name if e.department else "-", "score": p.risk_score,
            "level": p.risk_level, "role": e.job_role or "",
            "url": url_for("hr.employee_detail", emp_id=e.id)}


def answer(question, user):
    q = re.sub(r"\s+", " ", (question or "").lower()).strip()
    if not q:
        return {"answer": "Ask me something about attrition risk, departments, hiring or retention.", "items": [], "suggestions": SUGGESTIONS}

    emps = visible_employees(user)
    pm = latest_map(employee_ids=[e.id for e in emps])
    rows = [(e, pm[e.id]) for e in emps if e.id in pm]
    scope_note = {"manager": " (your team)", "tech_head": " (your department)"}.get(user.role, "")

    dept_names = [d.name for d in Department.query.all()]
    dept = next((d for d in dept_names if re.search(rf"\b{re.escape(d.lower())}\b", q)), None)
    level = "High" if re.search(r"\bhigh[- ]?risk\b|\bhigh\b|\bat risk\b|\brisky\b", q) else (
        "Medium" if "medium" in q else ("Low" if re.search(r"\blow[- ]risk\b|\bsafe\b", q) else None))
    if dept:
        rows = [(e, p) for e, p in rows if e.department and e.department.name == dept]

    if re.fullmatch(r"(hi|hello|hey|help|\?)", q):
        return {"answer": "Hi! I can query your live HR data. Try one of these:", "items": [], "suggestions": SUGGESTIONS}

    # --- department ranking -------------------------------------------------
    if "department" in q and re.search(r"highest|most|worst|riskiest|rank|which|compare", q):
        agg = defaultdict(list)
        for e, p in rows:
            agg[e.department.name if e.department else "Unassigned"].append(p)
        if not agg:
            return {"answer": "No prediction data available yet.", "items": [], "suggestions": SUGGESTIONS}
        ranked = sorted(agg.items(), key=lambda kv: -(sum(x.risk_level == "High" for x in kv[1]) / len(kv[1])))
        lines = [f"{i}. {n}: {round(100 * sum(x.risk_level == 'High' for x in ps) / len(ps))}% high-risk, "
                 f"avg score {round(sum(x.risk_score for x in ps) / len(ps), 1)}/100 ({len(ps)} employees)"
                 for i, (n, ps) in enumerate(ranked, 1)]
        return {"answer": f"Department attrition-risk ranking{scope_note}:\n" + "\n".join(lines), "items": [], "suggestions": []}

    # --- trend --------------------------------------------------------------
    if "trend" in q:
        snaps = AttritionSnapshot.query.order_by(AttritionSnapshot.month.desc()).all()
        by_m = defaultdict(lambda: [0, 0.0, 0])
        for s in snaps:
            by_m[s.month][0] += s.headcount or 0
            by_m[s.month][1] += (s.avg_risk or 0) * (s.headcount or 0)
            by_m[s.month][2] += s.exits or 0
        months = sorted(by_m)[-6:]
        if not months:
            return {"answer": "No historical snapshots are available yet.", "items": [], "suggestions": []}
        lines = [f"{m.strftime('%b %Y')}: avg risk {round(by_m[m][1] / by_m[m][0], 1)}, {by_m[m][2]} exits" for m in months if by_m[m][0]]
        return {"answer": "Attrition trend (last 6 months):\n" + "\n".join(lines), "items": [], "suggestions": []}

    # --- top reasons --------------------------------------------------------
    if re.search(r"reason|driver|why|cause|factor", q):
        c = Counter()
        for e, p in rows:
            if p.risk_level in ("High", "Medium"):
                c.update(factors_of(p))
        if not c:
            return {"answer": "No at-risk employees found, so no drivers to report.", "items": [], "suggestions": []}
        lines = [f"• {k}: {v} employees" for k, v in c.most_common(6)]
        return {"answer": f"Top attrition drivers{scope_note}{' in ' + dept if dept else ''}:\n" + "\n".join(lines), "items": [], "suggestions": []}

    # --- averages -----------------------------------------------------------
    if re.search(r"average|avg|mean", q) and "salary" in q:
        sel = [e for e, p in rows if not level or p.risk_level == level]
        if not sel:
            return {"answer": "No matching employees.", "items": [], "suggestions": []}
        avg = sum(e.monthly_salary or 0 for e in sel) / len(sel)
        return {"answer": f"Average monthly salary of {len(sel)} {level.lower() + '-risk ' if level else ''}employees"
                          f"{' in ' + dept if dept else ''}: ₹{avg:,.0f}.", "items": [], "suggestions": []}

    # --- counts -------------------------------------------------------------
    if re.search(r"how many|count|number of|total", q):
        if "open job" in q or "opportunit" in q or "vacanc" in q:
            return {"answer": f"There are {Opportunity.query.filter_by(status='open').count()} open opportunities.", "items": [], "suggestions": []}
        if "offer" in q:
            return {"answer": f"{Offer.query.filter_by(status='pending').count()} offers are awaiting a response.", "items": [], "suggestions": []}
        if "interview" in q:
            return {"answer": f"{Interview.query.filter_by(result='pending').count()} interviews are pending.", "items": [], "suggestions": []}
        if "application" in q:
            return {"answer": f"There are {Application.query.count()} applications in total.", "items": [], "suggestions": []}
        if level:
            n = sum(1 for e, p in rows if p.risk_level == level)
            return {"answer": f"{n} employees{' in ' + dept if dept else ''}{scope_note} are classified as {level} attrition risk.", "items": [], "suggestions": []}
        return {"answer": f"There are {len(rows)} analysed employees{' in ' + dept if dept else ''}{scope_note}.", "items": [], "suggestions": []}

    # --- employee lists with composable filters -----------------------------
    wants_list = re.search(r"show|list|who|which employees|employees|people|staff|find", q)
    if wants_list:
        sel = rows
        tags = []
        if level:
            sel = [(e, p) for e, p in sel if p.risk_level == level]; tags.append(f"{level} risk")
        if re.search(r"low (job )?satisfaction|unsatisfied|unhappy", q):
            sel = [(e, p) for e, p in sel if (e.job_satisfaction or 4) <= 2]; tags.append("low job satisfaction")
        if re.search(r"overtime", q):
            sel = [(e, p) for e, p in sel if e.overtime]; tags.append("overtime")
        if re.search(r"promotion", q):
            sel = [(e, p) for e, p in sel if (e.years_since_promotion or 0) >= 3]; tags.append("no recent promotion")
        if re.search(r"low salary|underpaid|low pay", q):
            sel = [(e, p) for e, p in sel if (e.salary_hike_percent or 99) < 10]; tags.append("low salary growth")
        if re.search(r"work.?life|burn", q):
            sel = [(e, p) for e, p in sel if (e.work_life_balance or 4) <= 2]; tags.append("poor work-life balance")
        if dept:
            tags.append(dept)
        sel.sort(key=lambda r: -r[1].risk_score)
        if not sel:
            return {"answer": f"No employees match: {', '.join(tags) or 'your filters'}.", "items": [], "suggestions": []}
        return {"answer": f"{len(sel)} employee(s) match {', '.join(tags) or 'your query'}{scope_note}. "
                          f"Showing the top {min(len(sel), 10)} by risk:",
                "items": [_item(e, p) for e, p in sel[:10]], "suggestions": []}

    return {"answer": "I didn't quite get that. Here are some things I can answer:", "items": [], "suggestions": SUGGESTIONS}
