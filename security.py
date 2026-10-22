"""Cross-cutting security: CSRF protection, security headers, permission checks
and the audit-log helper."""
import secrets
from functools import wraps
from flask import session, request, abort, g, render_template
from flask_login import current_user
from markupsafe import Markup
from extensions import db

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def csrf_input():
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


def has_permission(user, perm):
    from models import RolePermission
    if not getattr(user, "is_authenticated", False):
        return False
    if user.role == "super_admin":
        return True
    cache = g.setdefault("_perm_cache", {})
    if user.role not in cache:
        cache[user.role] = {rp.permission for rp in RolePermission.query.filter_by(role=user.role)}
    return perm in cache[user.role]


def can(perm):
    return has_permission(current_user, perm)


def permission_required(*perms):
    """Fine-grained, permission-based action guard (all listed permissions needed)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_user.is_authenticated:
                abort(401)
            if not all(has_permission(current_user, p) for p in perms):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def log_action(action, category="general", user=None):
    """Append an audit-log row (caller commits)."""
    from models import ActivityLog
    u = user if user is not None else (current_user if current_user.is_authenticated else None)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) if request else None
    db.session.add(ActivityLog(user_id=getattr(u, "id", None), action=action[:255],
                               category=category, ip_address=(ip or "")[:64]))


def init_security(app):
    app.jinja_env.globals.update(csrf_input=csrf_input, csrf_token=csrf_token, can=can)

    @app.before_request
    def _csrf_and_session():
        session.permanent = True          # refreshes the idle-timeout on every request
        if request.method not in SAFE_METHODS:
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("_csrf", "")
            if not expected or not secrets.compare_digest(sent, expected):
                abort(400, description="Your session expired or the form was tampered with. "
                                       "Please go back, refresh the page and try again.")

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; font-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.endpoint and not request.endpoint.startswith("static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
