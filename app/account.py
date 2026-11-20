"""Self-service account page: GDPR rights of access, rectification,
portability and erasure, plus password changes for local accounts."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, url_for
from flask_login import current_user, login_required, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Optional

from . import audit, hpc, privacy, settings
from .extensions import db
from .models import ACTIVE_RUN_STATUSES, Workflow, WorkflowRun

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


class ClusterLinkForm(FlaskForm):
    unix_username = StringField("Cluster user name", validators=[DataRequired(), Length(max=32)], render_kw={"autocomplete": "username", "autocapitalize": "none", "spellcheck": "false"})
    submit = SubmitField("Create my DAGonWeb key")


class ClusterActionForm(FlaskForm):
    submit = SubmitField("Submit")


def _render(profile: ProfileForm | None = None, password: PasswordForm | None = None, delete: DeleteForm | None = None, status: int = 200, cluster: ClusterLinkForm | None = None):
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
        hpc_enabled=hpc.enabled() and current_user.can("workflows.run"),
        cluster_form=cluster or ClusterLinkForm(prefix="cluster", unix_username=current_user.unix_username or ""),
        cluster_action=ClusterActionForm(prefix="cluster_action"),
        authorized_keys_line=hpc.authorized_keys_line(current_user) if current_user.unix_public_key else "",
        cluster_host=settings.get("hpc_ssh_host"),
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


def _cluster_allowed() -> bool:
    if hpc.enabled() and current_user.can("workflows.run"):
        return True
    flash("Cluster execution is not enabled on this server.", "warning")
    return False


@bp.post("/cluster")
@login_required
def cluster_link():
    """Claim a Unix user name and issue the key to add to authorized_keys."""
    if not _cluster_allowed():
        return redirect(url_for("account.index"))
    form = ClusterLinkForm(prefix="cluster")
    if not form.validate_on_submit():
        return _render(cluster=form, status=400)
    if _active_cluster_runs():
        flash("Wait for your cluster runs to finish before changing the linked account.", "warning")
        return redirect(url_for("account.index"))
    try:
        hpc.start_link(current_user, form.unix_username.data)
    except ValueError as exc:
        form.unix_username.errors.append(str(exc))
        return _render(cluster=form, status=400)
    audit.record("account.cluster_key", current_user.unix_username, "key pair issued")
    db.session.commit()
    flash("Key created. Add the line below to ~/.ssh/authorized_keys on the cluster, then verify.", "info")
    return redirect(url_for("account.index", _anchor="clusterTitle"))


@bp.post("/cluster/verify")
@login_required
def cluster_verify():
    if not _cluster_allowed():
        return redirect(url_for("account.index"))
    if not ClusterActionForm(prefix="cluster_action").validate_on_submit():
        return redirect(url_for("account.index"))
    try:
        info = hpc.verify_link(current_user)
    except hpc.HpcError as exc:
        audit.record("account.cluster_verify_failed", current_user.unix_username or "", str(exc)[:500])
        db.session.commit()
        flash(f"Verification failed: {exc}", "danger")
        return redirect(url_for("account.index", _anchor="clusterTitle"))
    audit.record("account.cluster_linked", current_user.unix_username, f"uid {info.get('uid')}, runner {info.get('version')}")
    db.session.commit()
    flash(f"Cluster account {current_user.unix_username} linked. Your runs now execute as this account.", "success")
    return redirect(url_for("account.index", _anchor="clusterTitle"))


@bp.post("/cluster/unlink")
@login_required
def cluster_unlink():
    if not ClusterActionForm(prefix="cluster_action").validate_on_submit():
        return redirect(url_for("account.index"))
    if _active_cluster_runs():
        flash("Wait for your cluster runs to finish before unlinking the account.", "warning")
        return redirect(url_for("account.index"))
    name = current_user.unix_username or ""
    hpc.unlink(current_user)
    audit.record("account.cluster_unlinked", name)
    db.session.commit()
    flash("Cluster account unlinked and the DAGonWeb key deleted. Also remove the dagonweb line from ~/.ssh/authorized_keys on the cluster.", "success")
    return redirect(url_for("account.index"))


def _active_cluster_runs() -> bool:
    return WorkflowRun.query.filter(WorkflowRun.user_id == current_user.id, WorkflowRun.unix_username.isnot(None), WorkflowRun.status.in_(ACTIVE_RUN_STATUSES)).count() > 0


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
