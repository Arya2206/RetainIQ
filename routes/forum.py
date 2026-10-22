from datetime import timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import func
from extensions import db, utcnow
from models import ForumPost, ForumComment, ForumLike, ForumCategory, User, notify
from security import has_permission, log_action

bp = Blueprint("forum", __name__, url_prefix="/forum")


@bp.route("/")
@login_required
def index():
    category, search = request.args.get("category", ""), request.args.get("q", "").strip()
    q = ForumPost.query
    if category:
        q = q.join(ForumCategory).filter(ForumCategory.name == category)
    if search:
        q = q.filter(ForumPost.title.ilike(f"%{search}%") | ForumPost.content.ilike(f"%{search}%"))
    posts = q.order_by(ForumPost.is_announcement.desc(), ForumPost.created_at.desc()).all()

    since = utcnow() - timedelta(days=30)
    score = {}
    for p in ForumPost.query.filter(ForumPost.created_at >= since, ForumPost.is_announcement.is_(False)).all():
        score[p.id] = p.like_count + 2 * p.comment_count
    trending_ids = [i for i, _ in sorted(score.items(), key=lambda kv: -kv[1])[:5] if score[i] > 0]
    trending = [db.session.get(ForumPost, i) for i in trending_ids]
    return render_template("forum/index.html", posts=posts, categories=ForumCategory.query.all(),
                           trending=trending, category=category, search=search)


@bp.route("/post/<int:post_id>")
@login_required
def view_post(post_id):
    post = db.get_or_404(ForumPost, post_id)
    liked = ForumLike.query.filter_by(post_id=post.id, user_id=current_user.id).first() is not None
    return render_template("forum/post.html", post=post, liked=liked,
                           comments=sorted(post.comments, key=lambda c: c.created_at))


@bp.route("/create", methods=["GET", "POST"])
@login_required
def create():
    cats = ForumCategory.query.all()
    if request.method == "POST":
        title, content = request.form.get("title", "").strip()[:200], request.form.get("content", "").strip()[:5000]
        cat = db.session.get(ForumCategory, int(request.form.get("category_id") or 0))
        if not title or not content:
            flash("A title and some content are required.", "danger")
            return render_template("forum/create.html", categories=cats, form=request.form)
        announce = bool(cat and cat.name == "Announcements")
        if announce and not has_permission(current_user, "post_announcements"):
            abort(403)
        anon = request.form.get("is_anonymous") == "on" or bool(cat and cat.name == "AnonymousFeedback")
        post = ForumPost(author_id=current_user.id, category_id=cat.id if cat else None, title=title,
                         content=content, is_anonymous=anon and not announce, is_announcement=announce)
        db.session.add(post)
        db.session.flush()
        if announce:
            for u in User.query.filter_by(is_active_flag=True).all():
                if u.id != current_user.id:
                    notify(u.id, f"Announcement: {title}", f"/forum/post/{post.id}", "forum")
        log_action(f"Forum {'announcement' if announce else 'post'}: {title}", "forum")
        db.session.commit()
        flash("Posted to the forum.", "success")
        return redirect(url_for("forum.view_post", post_id=post.id))
    return render_template("forum/create.html", categories=cats, form={})


@bp.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def comment(post_id):
    post = db.get_or_404(ForumPost, post_id)
    content = request.form.get("content", "").strip()[:2000]
    if not content:
        flash("Comment cannot be empty.", "danger")
        return redirect(url_for("forum.view_post", post_id=post.id))
    hr = current_user.role in ("hr", "super_admin")
    db.session.add(ForumComment(post_id=post.id, author_id=current_user.id, content=content, is_hr_response=hr))
    if post.author_id != current_user.id:
        who = "HR" if hr else current_user.name
        notify(post.author_id, f"{who} responded to your forum post.", f"/forum/post/{post.id}", "forum")
    db.session.commit()
    return redirect(url_for("forum.view_post", post_id=post.id))


@bp.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
def like(post_id):
    db.get_or_404(ForumPost, post_id)
    ex = ForumLike.query.filter_by(post_id=post_id, user_id=current_user.id).first()
    if ex:
        db.session.delete(ex)
    else:
        db.session.add(ForumLike(post_id=post_id, user_id=current_user.id))
    db.session.commit()
    return redirect(url_for("forum.view_post", post_id=post_id))


@bp.route("/post/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    post = db.get_or_404(ForumPost, post_id)
    if post.author_id != current_user.id and not has_permission(current_user, "moderate_forum"):
        abort(403)
    ForumComment.query.filter_by(post_id=post.id).delete()
    ForumLike.query.filter_by(post_id=post.id).delete()
    log_action(f"Deleted forum post '{post.title}'", "forum")
    db.session.delete(post)
    db.session.commit()
    flash("Post deleted.", "info")
    return redirect(url_for("forum.index"))
