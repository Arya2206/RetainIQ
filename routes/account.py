from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
from extensions import db
from models import Notification
from security import log_action
from routes.auth import validate_password

bp = Blueprint("account", __name__)


@bp.route("/account")
@login_required
def profile():
    return render_template("account/profile.html")


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def password():
    if request.method == "POST":
        cur, new, conf = (request.form.get(k, "") for k in ("current", "new", "confirm"))
        err = None
        if not current_user.check_password(cur):
            err = "Your current password is incorrect."
        elif validate_password(new):
            err = validate_password(new)
        elif new != conf:
            err = "The new passwords do not match."
        elif cur == new:
            err = "Choose a password different from the current one."
        if err:
            flash(err, "danger")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            log_action("Changed own password", "security")
            db.session.commit()
            flash("Password updated successfully.", "success")
            return redirect(url_for("auth.dashboard"))
    return render_template("account/password.html")


@bp.route("/notifications")
@login_required
def notifications():
    items = (Notification.query.filter_by(user_id=current_user.id)
             .order_by(Notification.created_at.desc()).limit(100).all())
    return render_template("account/notifications.html", items=items)


@bp.route("/notifications/<int:nid>/open")
@login_required
def open_notification(nid):
    n = db.session.get(Notification, nid)
    if not n or n.user_id != current_user.id:
        return redirect(url_for("account.notifications"))
    n.is_read = True
    db.session.commit()
    link = n.link if (n.link or "").startswith("/") and not (n.link or "").startswith("//") else None
    return redirect(link or url_for("account.notifications"))


@bp.route("/notifications/read-all", methods=["POST"])
@login_required
def read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(request.referrer or url_for("account.notifications"))
