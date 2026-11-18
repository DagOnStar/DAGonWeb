"""Self-service account page: GDPR rights of access, rectification,
portability and erasure, plus password changes for local accounts."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, url_for
from flask_login import current_user, login_required, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Optional

from . import audit, privacy
from .extensions import db
from .models import Workflow, WorkflowRun

bp = Blueprint("account", __name__)


class ProfileForm(FlaskForm):
    name = StringField("Display name", validators=[DataRequired(), Length(max=255)], render_kw={"autocomplete": "name"})
    submit = SubmitField("Save profile")


class PasswordForm(FlaskForm):
    current = PasswordField("Current password", validators=[DataRequired()], render_kw={"autocomplete": "current-password"})
    password = PasswordField("New password", validators=[DataRequired(), Length(min=10)], render_kw={"autocomplete": "new-password"})
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", message="The passwords do not match.")], render_kw={"autocomplete": "new-password"})
    submit = SubmitField("Change password")


class DeleteForm(FlaskForm):
    confirm_email = StringField("Type your email address to confirm", validators=[DataRequired()], render_kw={"autocomplete": "off"})
    password = PasswordField("Password", validators=[Optional()], render_kw={"autocomplete": "current-password"})
    submit = SubmitField("Delete my account and data")


def _render(profile: ProfileForm | None = None, password: PasswordForm | None = None, delete: DeleteForm | None = None, status: int = 200):
    stats = {
        "workflows": Workflow.query.filter_by(owner_id=current_user.id).count(),
        "runs": WorkflowRun.query.filter_by(user_id=current_user.id).count(),
    }
    return render_template(
        "account/index.html",
        profile=profile or ProfileForm(prefix="profile", name=current_user.name),
        password_form=password or PasswordForm(prefix="password"),
        delete_form=delete or DeleteForm(prefix="delete"),
        stats=stats,
        local=current_user.auth_provider == "local",
    ), status


@bp.get("/")
@login_required
def index():
    return _render()


@bp.post("/profile")
@login_required
def update_profile():
    form = ProfileForm(prefix="profile")
    if not form.validate_on_submit():
        return _render(profile=form, status=400)
    current_user.name = form.name.data.strip()
    audit.record("account.profile", current_user.email, "display name changed")
    db.session.commit()
    flash("Profile saved.", "success")
    return redirect(url_for("account.index"))


@bp.post("/password")
@login_required
def change_password():
    form = PasswordForm(prefix="password")
    if current_user.auth_provider != "local":
        flash("Your account signs in with single sign-on; manage the password with your provider.", "info")
        return redirect(url_for("account.index"))
    if not form.validate_on_submit():
        return _render(password=form, status=400)
    if not current_user.check_password(form.current.data):
        form.current.errors.append("The current password is not correct.")
        return _render(password=form, status=400)
    current_user.set_password(form.password.data)
    audit.record("account.password", current_user.email)
    db.session.commit()
    flash("Password changed.", "success")
    return redirect(url_for("account.index"))


@bp.get("/export")
@login_required
def export():
    """Right of access and data portability: everything stored about the user."""
    audit.record("account.export", current_user.email)
    db.session.commit()
    filename = f"dagonweb-personal-data-{datetime.now():%Y%m%d}.json"
    return Response(privacy.export_json(current_user), mimetype="application/json", headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.post("/delete")
@login_required
def delete():
    """Right to erasure."""
    form = DeleteForm(prefix="delete")
    if not form.validate_on_submit():
        return _render(delete=form, status=400)
    if form.confirm_email.data.strip().lower() != current_user.email.lower():
        form.confirm_email.errors.append("This does not match your email address.")
        return _render(delete=form, status=400)
    if current_user.auth_provider == "local" and not current_user.check_password(form.password.data or ""):
        form.password.errors.append("Enter your password to confirm.")
        return _render(delete=form, status=400)
    user = current_user._get_current_object()
    try:
        privacy.erase_user(user, by="self")
    except privacy.ErasureRefused as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("account.index"))
    db.session.commit()
    logout_user()
    flash("Your account and its data have been deleted.", "success")
    return redirect(url_for("index"))
