import json
from datetime import date, datetime
from functools import wraps
from flask import abort
from flask_login import current_user


def roles_required(*roles):
    """Coarse RBAC: restrict an *area* (blueprint/route) to certain roles."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def risk_badge(level):
    return {"High": "danger", "Medium": "warning", "Low": "success"}.get(level, "secondary")


SKILL_LEVELS = ("advanced", "basic", "none")


def parse_skills(skill_str):
    """'Python:advanced,SQL:basic' -> {'Python': 'advanced', 'SQL': 'basic'}"""
    result = {}
    for part in (skill_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, v = part.split(":", 1)
            k, v = k.strip(), v.strip().lower()
            if k:
                result[k] = v if v in SKILL_LEVELS else "basic"
        else:
            result[part] = "basic"
    return result


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except (TypeError, ValueError):
        return None


def _loads(s):
    try:
        return json.loads(s or "[]")
    except ValueError:
        return []


def factors_of(pred):
    return _loads(pred.top_factors) if pred else []


def recs_of(pred):
    return _loads(pred.recommendations) if pred else []


def explanations_of(pred):
    return _loads(pred.explanations) if pred else []


def latest_map(as_of=None, employee_ids=None):
    """{employee_id: latest AttritionPrediction}, optionally as of a date."""
    from models import AttritionPrediction
    q = AttritionPrediction.query.order_by(AttritionPrediction.created_at.asc(), AttritionPrediction.id.asc())
    if employee_ids is not None:
        q = q.filter(AttritionPrediction.employee_id.in_(list(employee_ids) or [-1]))
    result = {}
    for p in q.all():
        if as_of and p.created_at.date() > as_of:
            continue
        result[p.employee_id] = p
    return result


def latest_prediction(employee_id):
    from models import AttritionPrediction
    return (AttritionPrediction.query.filter_by(employee_id=employee_id)
            .order_by(AttritionPrediction.created_at.desc(), AttritionPrediction.id.desc()).first())


def latest_rows(employees=None):
    from models import Employee
    emps = employees if employees is not None else Employee.query.all()
    pm = latest_map(employee_ids=[e.id for e in emps])
    return [(e, pm[e.id]) for e in emps if e.id in pm]


def visible_employees(user):
    """Row-level data scoping: which employees may this user see?"""
    from models import Employee, User
    if user.role in ("super_admin", "hr", "ceo"):
        return Employee.query.all()
    if user.role == "manager":
        return (Employee.query.join(User, Employee.user_id == User.id)
                .filter(User.manager_id == user.id).all())
    if user.role == "tech_head":
        q = Employee.query
        if user.department_id:
            q = q.filter(Employee.department_id == user.department_id)
        return q.all()
    return []


def can_view_employee(user, emp):
    if user.role in ("super_admin", "hr", "ceo"):
        return True
    if user.role == "manager":
        return bool(emp.user and emp.user.manager_id == user.id)
    if user.role == "tech_head":
        return not user.department_id or emp.department_id == user.department_id
    return False


def inr(value):
    v = float(value or 0)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def replacement_cost(emp):
    """Industry rule of thumb: replacing an employee costs ~150% of annual salary."""
    return 1.5 * emp.annual_salary
