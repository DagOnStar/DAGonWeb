"""GDPR support: cookie inventory, consent, data export, erasure and retention.

DAGonWeb keeps personal data to the minimum needed to run the service:
accounts (email, name, password hash or SSO subject, roles, last sign-in,
privacy-notice acceptance), the workflows and runs a user creates, and an
audit log of security events (with the client IP address). Everything here
is used by the account page, the administration panel and the CLI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import settings
from .extensions import db
from .models import ACTIVE_RUN_STATUSES, AuditLog, RegistrationToken, Role, User, Workflow, WorkflowRun

CONSENT_COOKIE = "dw_consent"
CONSENT_MAX_AGE_DAYS = 180
COOKIE_CATEGORIES = (
    ("necessary", "Strictly necessary", "Sign-in, security (CSRF protection), your cookie choice, and drafts you are editing. The site cannot work without them, so they cannot be turned off."),
    ("preferences", "Preferences", "Remember your color theme and panel widths in this browser. Off unless you allow them."),
)


@dataclass(frozen=True)
class StorageItem:
    name: str
    category: str
    kind: str  # cookie, localStorage, sessionStorage
    purpose: str
    duration: str


STORAGE_ITEMS: tuple[StorageItem, ...] = (
    StorageItem("session", "necessary", "cookie", "Keeps you signed in and protects forms against cross-site request forgery (CSRF); also holds the single sign-on state during a sign-in.", "Until you close the browser"),
    StorageItem("remember_token", "necessary", "cookie", "Keeps you signed in across browser restarts, only if you tick “Remember me”.", "30 days"),
    StorageItem(CONSENT_COOKIE, "necessary", "cookie", "Stores your cookie choice so the banner is not shown again.", f"{CONSENT_MAX_AGE_DAYS} days"),
    StorageItem("dagonweb.playground.*", "necessary", "sessionStorage", "The workflow you are editing in the guest playground (never sent to the server except for a dry run you start).", "Until you close the tab"),
    StorageItem("dagonweb.assistant.*", "necessary", "sessionStorage", "The conversation with the workflow assistant.", "Until you close the tab"),
    StorageItem("dagonweb.theme", "preferences", "localStorage", "Your light, dark or automatic theme.", "Until you clear it or withdraw consent"),
    StorageItem("dagonweb.inspector-width", "preferences", "localStorage", "The width of the editor's Inspector panel.", "Until you clear it or withdraw consent"),
    StorageItem("dagonweb.run-details-width", "preferences", "localStorage", "The width of the run page's details panel.", "Until you clear it or withdraw consent"),
)

THIRD_PARTY_ASSETS = (
    ("cdn.jsdelivr.net", "Bootstrap style sheet and scripts"),
    ("cdnjs.cloudflare.com", "Monaco code editor and Chart.js"),
)


def controller_name() -> str:
    return str(settings.get("controller_name") or settings.get("site_name"))


def privacy_contact() -> str:
    return str(settings.get("privacy_contact_email") or settings.get("dpo_email") or "")


def current_policy_version() -> str:
    return str(settings.get("privacy_policy_version") or "1")


def needs_acceptance(user: User) -> bool:
    return user.privacy_version != current_policy_version()


def accept_policy(user: User) -> None:
    """Record that the user read the current privacy notice (caller commits)."""
    user.privacy_version = current_policy_version()
    user.privacy_accepted_at = _now()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------- export (Art. 15, 20)


def export_user_data(user: User) -> dict[str, Any]:
    """Everything DAGonWeb stores about ``user``, in a portable JSON structure."""
    workflows = Workflow.query.filter_by(owner_id=user.id).order_by(Workflow.id).all()
    runs = WorkflowRun.query.filter_by(user_id=user.id).order_by(WorkflowRun.id).all()
    audit = AuditLog.query.filter_by(user_id=user.id).order_by(AuditLog.id).all()
    return {
        "format": "dagonweb-personal-data/1",
        "exported_at": _now().isoformat(),
        "controller": {"name": controller_name(), "contact": privacy_contact()},
        "account": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "active": user.active,
            "auth_provider": user.auth_provider,
            "external_id": user.external_id,
            "roles": [role.name for role in user.roles],
            "created_at": _iso(user.created_at),
            "updated_at": _iso(user.updated_at),
            "last_login_at": _iso(user.last_login_at),
            "privacy_version": user.privacy_version,
            "privacy_accepted_at": _iso(user.privacy_accepted_at),
            # The private key is never exported: it is a credential, not data about the user.
            "cluster_account": {
                "unix_username": user.unix_username,
                "public_key": user.unix_public_key,
                "linked_at": _iso(user.unix_linked_at),
            } if user.unix_username else None,
        },
        "workflows": [
            {"id": workflow.id, "name": workflow.name, "description": workflow.description, "is_public": workflow.is_public,
             "created_at": _iso(workflow.created_at), "updated_at": _iso(workflow.updated_at), "document": workflow.as_json()}
            for workflow in workflows
        ],
        "runs": [
            {"id": run.id, "workflow_id": run.workflow_id, "label": run.label, "status": run.status,
             "created_at": _iso(run.created_at), "updated_at": _iso(run.updated_at), "log": run.log or "", "cluster_account": run.unix_username,
             "tasks": [{"name": task.task_uid, "status": task.status, "log": task.log or ""} for task in run.task_runs]}
            for run in runs
        ],
        "audit_log": [
            {"at": _iso(entry.created_at), "action": entry.action, "target": entry.target, "detail": entry.detail, "ip_address": entry.ip_address}
            for entry in audit
        ],
        "note": "Run artifacts (files in the scratch directory) can be downloaded from each run page.",
    }


def export_json(user: User) -> str:
    return json.dumps(export_user_data(user), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- erasure (Art. 17)


class ErasureRefused(ValueError):
    """The account cannot be erased right now (the message says why)."""


def _other_active_admins(user: User) -> int:
    return User.query.join(User.roles).filter(Role.name == "admin", User.active.is_(True), User.id != user.id).count()


def erase_user(user: User, *, by: str = "self") -> None:
    """Delete an account with its workflows, runs and scratch files.

    Audit entries are kept for their retention period (security, GDPR
    Art. 6(1)(f)) but detached from the account: the user link, the IP
    address and the email address are removed. The caller commits.
    """
    from .workflows.routes import remove_run_scratch

    if user.is_admin and not _other_active_admins(user):
        raise ErasureRefused("This is the last active administrator. Promote another administrator first.")
    owned = Workflow.query.filter_by(owner_id=user.id).all()
    runs = {run.id: run for run in WorkflowRun.query.filter_by(user_id=user.id).all()}
    for workflow in owned:
        for run in workflow.runs:
            runs[run.id] = run
    if any(run.status in ACTIVE_RUN_STATUSES for run in runs.values()):
        raise ErasureRefused("Some runs are still active. Wait for them to finish (or stop them), then try again.")
    for run in runs.values():
        remove_run_scratch(run)
        if run.workflow and run.workflow.owner_id != user.id:
            db.session.delete(run)
    for workflow in owned:
        db.session.delete(workflow)
    db.session.flush()
    pseudonym = f"deleted user #{user.id}"
    AuditLog.query.filter_by(user_id=user.id).update({"user_id": None, "ip_address": ""}, synchronize_session=False)
    AuditLog.query.filter_by(target=user.email).update({"target": pseudonym}, synchronize_session=False)
    RegistrationToken.query.filter_by(email=user.email).delete(synchronize_session=False)
    # Written directly: audit.record() would attach the requester's account and IP.
    db.session.add(AuditLog(user_id=None, action="account.erased", target=pseudonym, detail=f"by {by}; {len(owned)} workflow(s), {len(runs)} run(s)", ip_address=""))
    db.session.delete(user)


# ---------------------------------------------------------------- retention (Art. 5(1)(e))


def purge_audit_log(days: int) -> int:
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    removed = AuditLog.query.filter(AuditLog.created_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return int(removed or 0)


def purge_registration_tokens() -> int:
    """Remove used or expired registration links (they contain email addresses)."""
    removed = RegistrationToken.query.filter((RegistrationToken.used_at.isnot(None)) | (RegistrationToken.expires_at < _now())).delete(synchronize_session=False)
    db.session.commit()
    return int(removed or 0)


def inactive_accounts(days: int) -> list[User]:
    if days <= 0:
        return []
    cutoff = _now() - timedelta(days=days)
    return User.query.filter(db.func.coalesce(User.last_login_at, User.created_at) < cutoff).order_by(User.id).all()
