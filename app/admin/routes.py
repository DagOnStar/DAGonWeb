from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from ..extensions import db
from ..models import Role, Setting, User, Workflow
from .forms import RoleForm, SettingsForm, UserForm

bp = Blueprint("admin", __name__)

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.has_role("admin"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

@bp.route("/")
@admin_required
def dashboard():
    return render_template("admin/dashboard.html", users=User.query.count(), roles=Role.query.count(), workflows=Workflow.query.count())

@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    setting = Setting.query.filter_by(key="scratch_dir").first() or Setting(key="scratch_dir", value="/scratch/dagonweb")
    form = SettingsForm(scratch_dir=setting.value)
    if form.validate_on_submit():
        setting.value = form.scratch_dir.data
        db.session.add(setting)
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", form=form)

@bp.route("/roles")
@admin_required
def roles():
    return render_template("admin/roles.html", roles=Role.query.order_by(Role.name).all())

@bp.route("/roles/new", methods=["GET", "POST"])
@admin_required
def role_new():
    form = RoleForm()
    if form.validate_on_submit():
        db.session.add(Role(name=form.name.data, description=form.description.data or ""))
        db.session.commit()
        flash("Role created.", "success")
        return redirect(url_for("admin.roles"))
    return render_template("admin/role_form.html", form=form)

@bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@admin_required
def role_edit(role_id):
    role = db.get_or_404(Role, role_id)
    form = RoleForm(obj=role)
    if form.validate_on_submit():
        role.name = form.name.data
        role.description = form.description.data or ""
        db.session.commit()
        flash("Role updated.", "success")
        return redirect(url_for("admin.roles"))
    return render_template("admin/role_form.html", form=form)

@bp.post("/roles/<int:role_id>/delete")
@admin_required
def role_delete(role_id):
    role = db.get_or_404(Role, role_id)
    if role.name in {"admin", "user"}:
        flash("Built-in roles cannot be deleted.", "warning")
    else:
        db.session.delete(role)
        db.session.commit()
        flash("Role deleted.", "success")
    return redirect(url_for("admin.roles"))

@bp.route("/users")
@admin_required
def users():
    return render_template("admin/users.html", users=User.query.order_by(User.email).all())

def fill_role_choices(form: UserForm) -> None:
    form.roles.choices = [(role.id, role.name) for role in Role.query.order_by(Role.name).all()]

@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    form = UserForm()
    fill_role_choices(form)
    if form.validate_on_submit():
        user = User(name=form.name.data, email=form.email.data.lower(), active=form.active.data)
        user.set_password(form.password.data or "changeme123")
        user.roles = Role.query.filter(Role.id.in_(form.roles.data)).all()
        db.session.add(user)
        db.session.commit()
        flash("User created.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form)

@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(user_id):
    user = db.get_or_404(User, user_id)
    form = UserForm(obj=user)
    fill_role_choices(form)
    if not form.is_submitted():
        form.roles.data = [role.id for role in user.roles]
    if form.validate_on_submit():
        user.name = form.name.data
        user.email = form.email.data.lower()
        user.active = form.active.data
        if form.password.data:
            user.set_password(form.password.data)
        user.roles = Role.query.filter(Role.id.in_(form.roles.data)).all()
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form)

@bp.post("/users/<int:user_id>/delete")
@admin_required
def user_delete(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "warning")
    else:
        db.session.delete(user)
        db.session.commit()
        flash("User deleted.", "success")
    return redirect(url_for("admin.users"))
