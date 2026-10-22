import os
from flask import Flask, render_template, redirect, url_for, request, flash
from flask_login import current_user

from config import Config
from extensions import db, login_manager
from models import User, Notification, RolePermission, DEFAULT_ROLE_PERMISSIONS
from security import init_security, has_permission
from utils import inr


def _nav_for(user):
    """Navigation per role. Items: (endpoint, label, icon[, permission])."""
    I = lambda ep, label, icon, perm=None: {"endpoint": ep, "label": label, "icon": icon, "perm": perm}
    forum = I("forum.index", "Forum", "chat-dots")
    r = user.role
    if r == "super_admin":
        return [I("admin.dashboard", "Dashboard", "speedometer2"), I("admin.users", "Users", "people"),
                I("admin.permissions", "Permissions", "shield-lock"),
                {"label": "Manage", "icon": "gear", "children": [
                    I("admin.departments", "Departments", "diagram-3"), I("admin.logs", "Audit Log", "clock-history")]},
                {"label": "AI & Analytics", "icon": "cpu", "children": [
                    I("hr.dashboard", "AI Attrition Dashboard", "robot"), I("hr.upload", "Excel Upload", "file-earmark-excel"),
                    I("hr.model_insights", "Model Insights", "bar-chart-line"), I("ceo.dashboard", "Executive View", "graph-up")]},
                I("recruiter.dashboard", "Hiring", "briefcase"), forum]
    if r == "ceo":
        return [I("ceo.dashboard", "Executive Overview", "graph-up"), forum]
    if r == "tech_head":
        return [I("techhead.dashboard", "Tech Dashboard", "hdd-rack"), forum]
    if r == "hr":
        return [I("hr.dashboard", "AI Dashboard", "robot"), I("hr.employees", "Employees", "people"),
                I("hr.upload", "Excel Upload", "file-earmark-excel", "upload_excel"),
                {"label": "Recruitment", "icon": "briefcase", "children": [
                    I("recruiter.dashboard", "Opportunities", "megaphone"), I("recruiter.interviews", "Interviews", "calendar-check"),
                    I("recruiter.offers", "Offers", "envelope-paper")]},
                I("hr.model_insights", "Model", "bar-chart-line"), forum]
    if r == "manager":
        return [I("manager.dashboard", "My Team", "people"), forum]
    if r == "recruiter":
        return [I("recruiter.dashboard", "Dashboard", "speedometer2"),
                I("recruiter.create_opportunity", "Post Opportunity", "plus-circle", "manage_recruitment"),
                I("recruiter.interviews", "Interviews", "calendar-check"), I("recruiter.offers", "Offers", "envelope-paper"), forum]
    return [I("employee.dashboard", "My Profile", "person"), I("employee.career", "Career Growth", "graph-up-arrow"),
            I("employee.opportunities", "Opportunities", "briefcase"), I("employee.applications", "Applications", "file-earmark-text"),
            I("employee.interviews", "Interviews", "calendar-check"), I("employee.offers", "Offers", "envelope-paper"), forum]


def seed_permissions():
    if RolePermission.query.count() == 0:
        for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
            if role == "super_admin":
                continue
            for p in perms:
                db.session.add(RolePermission(role=role, permission=p))
        db.session.commit()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    app.jinja_env.filters["inr"] = inr
    init_security(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from routes import auth, account, admin, hr, manager, employee, recruiter, forum, api, executive, techhead, retention
    for m in (auth, account, admin, executive, techhead, hr, manager, employee, recruiter, forum, retention, api):
        app.register_blueprint(m.bp)

    @app.before_request
    def force_password_change():
        if (current_user.is_authenticated and current_user.must_change_password and request.endpoint
                and request.endpoint not in ("account.password", "auth.logout") and not request.endpoint.startswith("static")):
            flash("Please set a new password before continuing.", "warning")
            return redirect(url_for("account.password"))

    @app.context_processor
    def inject_globals():
        if not current_user.is_authenticated:
            return {"nav_items": [], "nav_unread_count": 0, "nav_notifications": []}
        notifs = (Notification.query.filter_by(user_id=current_user.id, is_read=False)
                  .order_by(Notification.created_at.desc()).limit(6).all())
        total_unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()

        def visible(item):
            return not item.get("perm") or has_permission(current_user, item["perm"])
        nav = []
        for it in _nav_for(current_user):
            if "children" in it:
                kids = [c for c in it["children"] if visible(c)]
                if kids:
                    nav.append({**it, "children": kids})
            elif visible(it):
                nav.append(it)
        return {"nav_items": nav, "nav_unread_count": total_unread, "nav_notifications": notifs,
                "can_assistant": has_permission(current_user, "use_ai_assistant")}

    def _err(code, title, msg):
        return render_template("errors/error.html", code=code, title=title, msg=msg), code

    @app.errorhandler(400)
    def e400(e):
        return _err(400, "Request could not be verified", getattr(e, "description", "Bad request."))

    @app.errorhandler(401)
    def e401(e):
        return redirect(url_for("auth.login", next=request.path))

    @app.errorhandler(403)
    def e403(e):
        return _err(403, "Access denied", "Your role or permissions do not allow you to open this page.")

    @app.errorhandler(404)
    def e404(e):
        return _err(404, "Page not found", "The page you are looking for does not exist.")

    @app.errorhandler(413)
    def e413(e):
        return _err(413, "File too large", "Uploads are limited to 10 MB.")

    @app.errorhandler(500)
    def e500(e):
        db.session.rollback()
        return _err(500, "Something went wrong", "An unexpected error occurred. It has been logged.")

    @app.cli.command("seed")
    def seed_command():
        """Reset the database with demo data:  flask seed"""
        from seed import run_seed
        run_seed(reset=True)

    with app.app_context():
        db.create_all()
        seed_permissions()
        if User.query.count() == 0:
            from seed import run_seed
            run_seed(reset=False)
    return app


app = create_app()

if __name__ == "__main__":
    print("\n  RetainIQ is running  ->  open http://127.0.0.1:5000 in your browser\n")
    app.run(debug=False, host="127.0.0.1", port=5000)
