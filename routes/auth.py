import re
from datetime import timedelta
from urllib.parse import urlparse
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from extensions import db, utcnow
from models import User, Department, ROLES, ROLE_LABELS, create_default_employee
from security import log_action

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLE_ENDPOINT = {
    "super_admin": "admin.dashboard", "ceo": "ceo.dashboard", "tech_head": "techhead.dashboard",
    "hr": "hr.dashboard", "manager": "manager.dashboard", "employee": "employee.dashboard",
    "recruiter": "recruiter.dashboard",
}


def validate_password(pw):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Password must contain at least one letter and one number."
    return None


def _safe_next(target):
    if not target:
        return None
    p = urlparse(target)
    return target if (not p.netloc and not p.scheme and target.startswith("/") and not target.startswith("//")) else None


@bp.route("/")
def index():
    return redirect(url_for("auth.dashboard" if current_user.is_authenticated else "auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        cfg = current_app.config

        if user and user.is_locked:
            mins = max(1, int((user.locked_until - utcnow()).total_seconds() // 60) + 1)
            flash(f"Too many failed attempts. This account is locked for about {mins} more minute(s).", "danger")
            return render_template("login.html"), 429
        if user and user.check_password(password):
            if not user.is_active_flag:
                flash("This account has been disabled. Please contact your administrator.", "danger")
                return render_template("login.html")
            user.failed_login_count, user.locked_until, user.last_login_at = 0, None, utcnow()
            login_user(user)
            log_action("Signed in", "auth", user)
            db.session.commit()
            return redirect(_safe_next(request.args.get("next")) or url_for("auth.dashboard"))

        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= cfg["MAX_FAILED_LOGINS"]:
                user.locked_until = utcnow() + timedelta(minutes=cfg["LOCKOUT_MINUTES"])
                user.failed_login_count = 0
                log_action(f"Account locked after repeated failed sign-ins: {email}", "security", user)
            else:
                log_action(f"Failed sign-in attempt for {email}", "security", user)
            db.session.commit()
        else:
            log_action(f"Failed sign-in for unknown email {email[:80]}", "security")
            db.session.commit()
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))
    departments = Department.query.order_by(Department.name).all()
    role_choices = [(r, ROLE_LABELS[r]) for r in ["employee", "manager", "hr", "recruiter", "tech_head", "ceo", "super_admin"]]
    if request.method == "POST":
        f = request.form
        name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
        password, confirm = f.get("password", ""), f.get("confirm", "")
        role, dept_id = f.get("role", "employee"), f.get("department_id") or None
        code = f.get("access_code", "").strip()
        error = None
        if not name or len(name) > 120:
            error = "Please enter your full name."
        elif not EMAIL_RE.match(email) or len(email) > 120:
            error = "Please enter a valid email address."
        elif validate_password(password):
            error = validate_password(password)
        elif password != confirm:
            error = "Passwords do not match."
        elif role not in ROLES:
            error = "Please choose a valid role."
        elif role != "employee" and code != current_app.config["STAFF_ACCESS_CODE"]:
            error = "Invalid staff access code. Only Employee accounts can be created without one."
        elif User.query.filter_by(email=email).first():
            error = "An account with this email already exists. Try signing in."
        elif dept_id and not db.session.get(Department, int(dept_id)):
            error = "Please choose a valid department."
        if error:
            flash(error, "danger")
            return render_template("register.html", departments=departments, role_choices=role_choices, form=f)
        user = User(name=name, email=email, role=role, department_id=int(dept_id) if dept_id else None)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        if role == "employee":
            create_default_employee(user)
        log_action(f"New account registered: {email} as {ROLE_LABELS[role]}", "auth", user)
        db.session.commit()
        flash(f"Account created for {name}. Please sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("register.html", departments=departments, role_choices=role_choices, form={})


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    log_action("Signed out", "auth")
    db.session.commit()
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for(ROLE_ENDPOINT.get(current_user.role, "auth.login")))
