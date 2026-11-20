"""Administration panel: users, roles, settings sections, statistics and logs."""
from __future__ import annotations

import csv
import io
import shutil
from collections import Counter
from configparser import ConfigParser, Error as ConfigParserError
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.engine import make_url

from .. import audit, settings
from ..authz import admin_required
from ..branding import LOGO_TYPES, logo_path, remove_logo, save_logo
from ..dagon_ini import DEFAULT_DAGON_INI
from ..executor.dispatch import backend_status
from ..extensions import db
from ..mail import send_mail
from ..models import ACTIVE_RUN_STATUSES, PERMISSIONS, AuditLog, Role, User, Workflow, WorkflowRun
from .forms import DagonIniForm, RoleForm, UserForm

bp = Blueprint("admin", __name__)
PROTECTED_ROLES = {"admin", "user"}


@bp.context_processor
def admin_navigation() -> dict:
    return {"admin_sections": settings.SECTIONS}


# ---------------------------------------------------------------- overview


def run_statistics(days: int = 14) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days - 1)
    since = since.replace(hour=0, minute=0, second=0, microsecond=0)
    recent = WorkflowRun.query.filter(WorkflowRun.created_at >= since.replace(tzinfo=None)).all()
    per_day: dict[str, Counter] = {}
    for offset in range(days):
        per_day[(since + timedelta(days=offset)).strftime("%Y-%m-%d")] = Counter()
    for run in recent:
        key = run.created_at.strftime("%Y-%m-%d")
        if key in per_day:
            per_day[key][run.status] += 1
    peak = max([sum(counter.values()) for counter in per_day.values()] + [1])
    by_status = dict(db.session.query(WorkflowRun.status, func.count(WorkflowRun.id)).group_by(WorkflowRun.status).all())
    top_workflows = (
        db.session.query(Workflow.name, func.count(WorkflowRun.id).label("runs"))
        .join(WorkflowRun, WorkflowRun.workflow_id == Workflow.id)
        .group_by(Workflow.id, Workflow.name)
        .order_by(func.count(WorkflowRun.id).desc())
        .limit(5)
        .all()
    )
    top_users = (
        db.session.query(User.name, func.count(WorkflowRun.id).label("runs"))
        .join(WorkflowRun, WorkflowRun.user_id == User.id)
        .group_by(User.id, User.name)
        .order_by(func.count(WorkflowRun.id).desc())
        .limit(5)
        .all()
    )
    finished = by_status.get("success", 0) + by_status.get("failed", 0)
    return {
        "per_day": [{"day": day, "success": counter.get("success", 0), "failed": counter.get("failed", 0), "other": sum(counter.values()) - counter.get("success", 0) - counter.get("failed", 0), "total": sum(counter.values())} for day, counter in per_day.items()],
        "peak": peak,
        "by_status": by_status,
        "success_rate": round(100 * by_status.get("success", 0) / finished) if finished else None,
        "top_workflows": top_workflows,
        "top_users": top_users,
        "failures": WorkflowRun.query.filter_by(status="failed").order_by(WorkflowRun.updated_at.desc()).limit(5).all(),
        "active": WorkflowRun.query.filter(WorkflowRun.status.in_(ACTIVE_RUN_STATUSES)).count(),
    }


@bp.route("/")
@admin_required
def dashboard():
    counts = {
        "users": User.query.count(),
        "active_users": User.query.filter(User.last_login_at >= (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)).count(),
        "roles": Role.query.count(),
        "workflows": Workflow.query.count(),
        "runs": WorkflowRun.query.count(),
    }
    return render_template(
        "admin/dashboard.html",
        counts=counts,
        stats=run_statistics(),
        recent_events=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(8).all(),
        status=backend_status(),
    )


# ---------------------------------------------------------------- settings


@bp.route("/settings")
@admin_required
def settings_index():
    return redirect(url_for("admin.dashboard"))


@bp.route("/settings/<section>", methods=["GET", "POST"])
@admin_required
def settings_section(section: str):
    if section not in settings.SECTION_KEYS:
        abort(404)
    errors: dict[str, str] = {}
    if request.method == "POST":
        staged: dict[str, object] = {}
        for definition in settings.definitions(section):
            if settings.is_overridden(definition.key):
                continue  # pinned by the environment; the field is disabled
            raw = request.form.get(definition.key)
            if definition.secret:
                if request.form.get(f"{definition.key}__clear"):
                    staged[definition.key] = ""
                    continue
                if not raw:
                    continue  # keep the stored secret
            try:
                staged[definition.key] = settings.validate(definition, raw)
            except ValueError as exc:
                errors[definition.key] = str(exc)
        if section == "storage" and "scratch_dir" in staged and not str(staged["scratch_dir"]).strip():
            errors["scratch_dir"] = "The scratch directory is required."
        if section == "hpc" and staged.get("hpc_enabled", settings.get("hpc_enabled")):
            if not str(staged.get("hpc_ssh_host_key", settings.get("hpc_ssh_host_key")) or "").strip():
                errors["hpc_ssh_host_key"] = "The login node host key is required before cluster execution can be turned on."
        if not errors:
            for key, value in staged.items():
                settings.set_value(key, value)
            audit.record("admin.settings", section, ", ".join(sorted(key for key in staged if not settings.DEFINITIONS[key].secret)))
            db.session.commit()
            flash(f"{dict((key, label) for key, label, _ in settings.SECTIONS)[section]} settings saved.", "success")
            return redirect(url_for("admin.settings_section", section=section))
        flash("Please correct the highlighted fields.", "danger")
    context: dict = {}
    if section == "execution":
        context["status"] = backend_status()
    if section == "storage":
        context["storage"] = storage_overview()
    if section == "authentication":
        context["callback_urls"] = {key: url_for("auth.sso_callback", provider_key=key, _external=True) for key in ("google", "microsoft", "oidc")}
    if section == "appearance":
        context["has_logo"] = logo_path() is not None
        context["logo_types"] = ", ".join(sorted(LOGO_TYPES))
    return render_template(
        "admin/settings_section.html",
        section=section,
        title=dict((key, label) for key, label, _ in settings.SECTIONS)[section],
        description=dict((key, text) for key, _, text in settings.SECTIONS)[section],
        rows=settings.section_values(section),
        errors=errors,
        submitted=request.form if request.method == "POST" else None,
        **context,
    )


@bp.post("/settings/appearance/logo")
@admin_required
def upload_logo():
    if request.form.get("remove"):
        remove_logo()
        audit.commit("admin.logo", "removed")
        flash("Logo removed; the default DAGonWeb logo is used.", "success")
        return redirect(url_for("admin.settings_section", section="appearance"))
    uploaded = request.files.get("logo")
    try:
        if not uploaded or not uploaded.filename:
            raise ValueError("Choose an image file.")
        save_logo(uploaded)
    except ValueError as exc:
        flash(str(exc), "danger")
    else:
        audit.commit("admin.logo", uploaded.filename)
        flash("Logo updated.", "success")
    return redirect(url_for("admin.settings_section", section="appearance"))


@bp.post("/settings/smtp/test")
@admin_required
def test_smtp():
    try:
        sent = send_mail(current_user.email, f"{settings.get('site_name')} test message", "SMTP settings work: this message was sent from the administration panel.\n")
    except Exception as exc:
        flash(f"SMTP test failed: {exc}", "danger")
    else:
        flash(f"Test message sent to {current_user.email}." if sent else "SMTP host is not configured.", "success" if sent else "warning")
    return redirect(url_for("admin.settings_section", section="smtp"))


@bp.post("/settings/llm/test")
@admin_required
def test_llm():
    from ..agents.llm import LLMError, ping

    try:
        reply = ping()
    except LLMError as exc:
        flash(f"LLM connection failed: {exc}", "danger")
    else:
        flash(f"LLM connection works ({settings.get('llm_model')}): {reply[:120]}", "success")
    return redirect(url_for("admin.settings_section", section="llm"))


def storage_overview() -> dict:
    from ..executor.local import scratch_root

    root = scratch_root()
    usage = shutil.disk_usage(root)
    runs = WorkflowRun.query.count()
    database = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])
    return {
        "root": str(root),
        "free_gb": usage.free / 1024**3,
        "total_gb": usage.total / 1024**3,
        "used_pct": round(100 * (usage.total - usage.free) / usage.total) if usage.total else 0,
        "runs": runs,
        "database": database.render_as_string(hide_password=True),
        "database_backend": database.get_backend_name(),
    }


@bp.post("/storage/cleanup")
@admin_required
def storage_cleanup():
    from ..maintenance import cleanup_runs

    days = int(settings.get("run_retention_days") or 0)
    if days <= 0:
        flash("Set a retention period first.", "warning")
    else:
        removed = cleanup_runs(days)
        audit.commit("admin.cleanup", f"{removed} runs", f"older than {days} days")
        flash(f"Removed {removed} finished run(s) older than {days} days.", "success")
    return redirect(url_for("admin.settings_section", section="storage"))


# ---------------------------------------------------------------- dagon.ini


def dagon_ini_path() -> Path:
    """Return the one application-managed DAGonStar configuration file."""
    return Path(current_app.config["DAGON_INI_PATH"]).resolve()


@bp.route("/dagon-ini", methods=["GET", "POST"])
@admin_required
def dagon_ini():
    path = dagon_ini_path()
    content = path.read_text(encoding="utf-8") if path.is_file() else DEFAULT_DAGON_INI
    form = DagonIniForm(content=content)
    if form.validate_on_submit():
        content = form.content.data or ""
        if "\0" in content:
            form.content.errors.append("The configuration cannot contain null bytes.")
        else:
            parser = ConfigParser(interpolation=None)
            try:
                parser.read_string(content)
            except ConfigParserError as exc:
                form.content.errors.append(f"Invalid INI configuration: {exc}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = path.with_suffix(path.suffix + ".tmp")
                temporary_path.write_text(content.rstrip() + "\n", encoding="utf-8")
                temporary_path.replace(path)
                audit.commit("admin.dagon_ini", str(path.name))
                flash("dagon.ini saved. New workflow runs will use it.", "success")
                return redirect(url_for("admin.dagon_ini"))
    return render_template("admin/dagon_ini.html", form=form, path=path)


# ---------------------------------------------------------------- audit log


@bp.route("/logs")
@admin_required
def logs():
    query = AuditLog.query
    action = request.args.get("action", "").strip()
    who = request.args.get("user", "").strip()
    if action:
        query = query.filter(AuditLog.action.startswith(action))
    if who:
        query = query.join(User, AuditLog.user_id == User.id).filter(User.email.ilike(f"%{who}%"))
    page = max(1, request.args.get("page", 1, type=int))
    pagination = query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    actions = [row[0] for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()]
    return render_template("admin/logs.html", pagination=pagination, actions=actions, action=action, who=who)


@bp.get("/logs.csv")
@admin_required
def logs_csv():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time_utc", "user", "action", "target", "detail", "ip"])
    for entry in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(50_000):
        writer.writerow([entry.created_at.isoformat(), entry.user.email if entry.user else "", entry.action, entry.target, entry.detail, entry.ip_address])
    return Response(buffer.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="dagonweb-audit.csv"'})


# ---------------------------------------------------------------- roles


@bp.route("/roles")
@admin_required
def roles():
    return render_template("admin/roles.html", roles=Role.query.order_by(Role.name).all(), permissions=PERMISSIONS, protected=PROTECTED_ROLES)


def fill_permission_choices(form: RoleForm) -> None:
    form.permissions.choices = list(PERMISSIONS.items())


@bp.route("/roles/new", methods=["GET", "POST"])
@admin_required
def role_new():
    form = RoleForm()
    fill_permission_choices(form)
    if form.validate_on_submit():
        if Role.query.filter_by(name=form.name.data).first():
            form.name.errors.append("A role with this name exists.")
        else:
            role = Role(name=form.name.data, description=form.description.data or "")
            role.permissions = form.permissions.data or []
            db.session.add(role)
            audit.record("admin.role_create", role.name, ", ".join(role.permissions))
            db.session.commit()
            flash("Role created.", "success")
            return redirect(url_for("admin.roles"))
    return render_template("admin/role_form.html", form=form, title="New role")


@bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@admin_required
def role_edit(role_id: int):
    role = db.get_or_404(Role, role_id)
    form = RoleForm(obj=role)
    fill_permission_choices(form)
    if not form.is_submitted():
        form.permissions.data = role.permissions
    if form.validate_on_submit():
        if role.name in PROTECTED_ROLES and form.name.data != role.name:
            form.name.errors.append("Built-in roles cannot be renamed.")
        else:
            role.name = form.name.data
            role.description = form.description.data or ""
            role.permissions = form.permissions.data or []
            audit.record("admin.role_update", role.name, ", ".join(role.permissions))
            db.session.commit()
            flash("Role updated.", "success")
            return redirect(url_for("admin.roles"))
    return render_template("admin/role_form.html", form=form, title=f"Edit role {role.name}", role=role)


@bp.post("/roles/<int:role_id>/delete")
@admin_required
def role_delete(role_id: int):
    role = db.get_or_404(Role, role_id)
    if role.name in PROTECTED_ROLES:
        flash("Built-in roles cannot be deleted.", "warning")
    else:
        audit.record("admin.role_delete", role.name)
        db.session.delete(role)
        db.session.commit()
        flash("Role deleted.", "success")
    return redirect(url_for("admin.roles"))


# ---------------------------------------------------------------- users


@bp.route("/users")
@admin_required
def users():
    search = request.args.get("q", "").strip()
    query = User.query
    if search:
        query = query.filter((User.email.ilike(f"%{search}%")) | (User.name.ilike(f"%{search}%")))
    return render_template("admin/users.html", users=query.order_by(User.email).all(), search=search)


def fill_role_choices(form: UserForm) -> None:
    form.roles.choices = [(role.id, role.name) for role in Role.query.order_by(Role.name).all()]


def admin_count_without(user: User) -> int:
    return sum(1 for other in Role.query.filter_by(name="admin").one().users if other.id != user.id and other.active)


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    form = UserForm()
    fill_role_choices(form)
    if form.validate_on_submit():
        email = form.email.data.lower()
        if User.query.filter_by(email=email).first():
            form.email.errors.append("A user with this email exists.")
        elif not form.password.data:
            form.password.errors.append("Set an initial password (the user can be SSO-only by disabling local sign-in).")
        else:
            user = User(name=form.name.data, email=email, active=form.active.data)
            user.set_password(form.password.data)
            user.roles = Role.query.filter(Role.id.in_(form.roles.data)).all()
            db.session.add(user)
            db.session.flush()
            audit.record("admin.user_create", user.email, ", ".join(role.name for role in user.roles))
            db.session.commit()
            flash("User created.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, title="New user")


@bp.get("/users/<int:user_id>/export")
@admin_required
def user_export(user_id: int):
    """Answer a GDPR access or portability request for a user."""
    from .. import privacy

    user = db.get_or_404(User, user_id)
    audit.record("admin.user_export", user.email)
    db.session.commit()
    return Response(privacy.export_json(user), mimetype="application/json", headers={"Content-Disposition": f"attachment; filename=dagonweb-personal-data-user-{user.id}.json"})


@bp.post("/users/<int:user_id>/erase")
@admin_required
def user_erase(user_id: int):
    """Answer a GDPR erasure request: delete the account and everything it owns."""
    from .. import privacy

    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("Use your account page to delete your own account.", "warning")
        return redirect(url_for("admin.user_edit", user_id=user.id))
    if request.form.get("confirm") != "1":
        flash("Confirm the erasure first.", "warning")
        return redirect(url_for("admin.user_edit", user_id=user.id))
    try:
        privacy.erase_user(user, by=f"administrator #{current_user.id}")
    except privacy.ErasureRefused as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("admin.user_edit", user_id=user.id))
    db.session.commit()
    flash("The account and its data were erased.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(user_id: int):
    user = db.get_or_404(User, user_id)
    form = UserForm(obj=user)
    fill_role_choices(form)
    if not form.is_submitted():
        form.roles.data = [role.id for role in user.roles]
    if form.validate_on_submit():
        new_roles = Role.query.filter(Role.id.in_(form.roles.data)).all()
        email = form.email.data.lower()
        loses_admin = user.is_admin and (not any(role.name == "admin" for role in new_roles) or not form.active.data)
        if loses_admin and admin_count_without(user) == 0:
            flash("At least one active administrator must remain.", "danger")
        elif email != user.email and User.query.filter_by(email=email).first():
            form.email.errors.append("A user with this email exists.")
        else:
            user.name = form.name.data
            user.email = email
            user.active = form.active.data
            if form.password.data:
                user.set_password(form.password.data)
            if form.unlink_sso.data:
                user.auth_provider, user.external_id = "local", None
            user.roles = new_roles
            audit.record("admin.user_update", user.email, f"roles={','.join(role.name for role in new_roles)} active={user.active}")
            db.session.commit()
            flash("User updated.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, title=f"Edit {user.email}", user=user)


@bp.post("/users/<int:user_id>/delete")
@admin_required
def user_delete(user_id: int):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "warning")
    elif Workflow.query.filter_by(owner_id=user.id).count() or WorkflowRun.query.filter_by(user_id=user.id).count():
        flash("This user owns workflows or runs. Disable the account, or use “Erase account and data” on the user's page.", "warning")
    else:
        audit.record("admin.user_delete", user.email)
        db.session.delete(user)
        db.session.commit()
        flash("User deleted.", "success")
    return redirect(url_for("admin.users"))
