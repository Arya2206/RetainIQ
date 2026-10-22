import csv, io, secrets
from collections import Counter
from datetime import timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from flask_login import login_required, current_user
from sqlalchemy import func
from extensions import db, utcnow
from models import (User, Department, Employee, AttritionPrediction, ActivityLog, Opportunity, UploadBatch,
                    RolePermission, ROLES, ROLE_LABELS, PERMISSIONS, DEFAULT_ROLE_PERMISSIONS,
                    create_default_employee)
from security import log_action, permission_required
from utils import roles_required, latest_map

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
@login_required
@roles_required("super_admin")
def guard():
    pass


@bp.route("/")
def dashboard():
    pm = latest_map()
    levels = Counter(p.risk_level for p in pm.values())
    since = utcnow() - timedelta(days=13)
    logins = Counter(l.timestamp.strftime("%d %b") for l in
                     ActivityLog.query.filter(ActivityLog.action == "Signed in", ActivityLog.timestamp >= since).all())
    days = [(utcnow() - timedelta(days=i)).strftime("%d %b") for i in range(13, -1, -1)]
    failed = ActivityLog.query.filter(ActivityLog.category == "security",
                                      ActivityLog.timestamp >= utcnow() - timedelta(days=7)).count()
    return render_template(
        "admin/dashboard.html", total_users=User.query.count(), total_employees=Employee.query.count(),
        total_depts=Department.query.count(), open_jobs=Opportunity.query.filter_by(status="open").count(),
        uploads=UploadBatch.query.count(), failed=failed, locked=sum(1 for u in User.query.all() if u.is_locked),
        risk_counts=dict(levels), users_by_role={ROLE_LABELS[r]: n for r, n in
                                                 db.session.query(User.role, func.count(User.id)).group_by(User.role).all()},
        login_days=days, login_counts=[logins.get(d, 0) for d in days],
        recent_activity=ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(12).all())


@bp.route("/users")
@permission_required("manage_users")
def users():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    query = User.query
    if q:
        query = query.filter(User.name.ilike(f"%{q}%") | User.email.ilike(f"%{q}%"))
    if role in ROLES:
        query = query.filter_by(role=role)
    return render_template("admin/users.html", users=query.order_by(User.role, User.name).all(),
                           departments=Department.query.order_by(Department.name).all(),
                           role_choices=[(r, ROLE_LABELS[r]) for r in ROLES], q=q, role_filter=role,
                           managers=User.query.filter(User.role.in_(["manager", "tech_head"])).order_by(User.name).all())


@bp.route("/users/create", methods=["POST"])
@permission_required("manage_users")
def create_user():
    f = request.form
    name, email, role = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("role")
    dept_id = f.get("department_id") or None
    if not name or "@" not in email or role not in ROLES:
        flash("Please provide a name, a valid email and a role.", "danger")
        return redirect(url_for("admin.users"))
    if User.query.filter_by(email=email).first():
        flash("A user with that email already exists.", "danger")
        return redirect(url_for("admin.users"))
    temp = f.get("password") or secrets.token_urlsafe(9)
    u = User(name=name, email=email, role=role, department_id=int(dept_id) if dept_id else None,
             must_change_password=True)
    u.set_password(temp)
    db.session.add(u)
    db.session.flush()
    if role == "employee":
        create_default_employee(u)
    log_action(f"Created user {email} ({ROLE_LABELS[role]})", "admin")
    db.session.commit()
    flash(f"User {name} created. Temporary password (shown once): {temp} — they must change it at first sign-in.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/update", methods=["POST"])
@permission_required("manage_users")
def update_user(uid):
    u = db.get_or_404(User, uid)
    role, dept_id, mgr = request.form.get("role"), request.form.get("department_id") or None, request.form.get("manager_id") or None
    if u.id == current_user.id and role != u.role:
        flash("You cannot change your own role.", "danger")
        return redirect(url_for("admin.users"))
    changes = []
    if role in ROLES and role != u.role:
        changes.append(f"role {ROLE_LABELS[u.role]}→{ROLE_LABELS[role]}")
        u.role = role
        if role == "employee" and not u.employee_profile:
            create_default_employee(u)
    new_dept = int(dept_id) if dept_id else None
    if new_dept != u.department_id:
        u.department_id = new_dept
        if u.employee_profile:
            u.employee_profile.department_id = new_dept
        changes.append("department")
    new_mgr = int(mgr) if mgr else None
    if new_mgr != u.manager_id and new_mgr != u.id:
        u.manager_id = new_mgr
        changes.append("reporting manager")
    if changes:
        log_action(f"Updated {u.email}: " + ", ".join(changes), "admin")
        db.session.commit()
        flash(f"Updated {u.name}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/reset-password", methods=["POST"])
@permission_required("manage_users")
def reset_password(uid):
    u = db.get_or_404(User, uid)
    temp = secrets.token_urlsafe(9)
    u.set_password(temp)
    u.must_change_password = True
    u.failed_login_count, u.locked_until = 0, None
    log_action(f"Reset password for {u.email}", "security")
    db.session.commit()
    flash(f"Temporary password for {u.name} (shown once): {temp}", "warning")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/toggle", methods=["POST"])
@permission_required("manage_users")
def toggle_user(uid):
    u = db.get_or_404(User, uid)
    if u.id == current_user.id:
        flash("You cannot disable your own account.", "danger")
        return redirect(url_for("admin.users"))
    u.is_active_flag = not u.is_active_flag
    log_action(f"{'Enabled' if u.is_active_flag else 'Disabled'} account {u.email}", "admin")
    db.session.commit()
    flash(f"{u.name} {'enabled' if u.is_active_flag else 'disabled'}.", "info")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/unlock", methods=["POST"])
@permission_required("manage_users")
def unlock_user(uid):
    u = db.get_or_404(User, uid)
    u.failed_login_count, u.locked_until = 0, None
    log_action(f"Unlocked account {u.email}", "security")
    db.session.commit()
    flash(f"{u.name} unlocked.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/permissions", methods=["GET", "POST"])
@permission_required("manage_permissions")
def permissions():
    editable = [r for r in ROLES if r != "super_admin"]
    if request.method == "POST":
        if request.form.get("reset") == "1":
            wanted = {r: set(DEFAULT_ROLE_PERMISSIONS[r]) for r in editable}
        else:
            wanted = {r: {p for p in PERMISSIONS if request.form.get(f"{r}:{p}")} for r in editable}
        RolePermission.query.filter(RolePermission.role.in_(editable)).delete(synchronize_session=False)
        for r, perms in wanted.items():
            for p in perms:
                db.session.add(RolePermission(role=r, permission=p))
        log_action("Updated the role-permission matrix" + (" (reset to defaults)" if request.form.get("reset") == "1" else ""), "security")
        db.session.commit()
        flash("Permissions saved. Changes apply immediately.", "success")
        return redirect(url_for("admin.permissions"))
    granted = {(rp.role, rp.permission) for rp in RolePermission.query.all()}
    return render_template("admin/permissions.html", roles=ROLES, editable=editable,
                           role_labels=ROLE_LABELS, permissions=PERMISSIONS, granted=granted)


@bp.route("/departments", methods=["GET", "POST"])
@permission_required("manage_departments")
def departments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        if name and not Department.query.filter(func.lower(Department.name) == name.lower()).first():
            db.session.add(Department(name=name))
            log_action(f"Created department {name}", "admin")
            db.session.commit()
            flash("Department added.", "success")
        else:
            flash("Department already exists or the name is invalid.", "danger")
        return redirect(url_for("admin.departments"))
    counts = dict(db.session.query(Employee.department_id, func.count(Employee.id)).group_by(Employee.department_id).all())
    return render_template("admin/departments.html", departments=Department.query.order_by(Department.name).all(), counts=counts)


@bp.route("/logs")
@permission_required("view_activity_logs")
def logs():
    cat, q = request.args.get("category", ""), request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    query = ActivityLog.query
    if cat:
        query = query.filter_by(category=cat)
    if q:
        query = query.filter(ActivityLog.action.ilike(f"%{q}%"))
    total = query.count()
    rows = query.order_by(ActivityLog.timestamp.desc()).offset((page - 1) * 25).limit(25).all()
    cats = [c for (c,) in db.session.query(ActivityLog.category).distinct().all() if c]
    return render_template("admin/logs.html", logs=rows, cats=sorted(cats), cat=cat, q=q, page=page,
                           pages=max(1, -(-total // 25)), total=total)


@bp.route("/logs.csv")
@permission_required("view_activity_logs")
def logs_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "user", "category", "action", "ip"])
    for l in ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(5000):
        w.writerow([l.timestamp.isoformat(sep=" ", timespec="seconds"), l.user.email if l.user else "system",
                    l.category, l.action, l.ip_address])
    log_action("Exported the activity log", "data")
    db.session.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=retainiq_activity_log.csv"})
