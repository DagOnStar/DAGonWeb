"""Audit log helper: one call records who did what, from where."""
from __future__ import annotations

from flask import has_request_context, request
from flask_login import current_user

from .extensions import db
from .models import AuditLog


def record(action: str, target: str = "", detail: str = "", user_id: int | None = None) -> None:
    """Stage an audit entry in the current session (the caller commits)."""
    if user_id is None and has_request_context() and current_user and current_user.is_authenticated:
        user_id = current_user.id
    ip_address = ""
    if has_request_context():
        ip_address = (request.remote_addr or "")[:64]
    db.session.add(AuditLog(user_id=user_id, action=action[:80], target=str(target)[:255], detail=str(detail)[:4000], ip_address=ip_address))


def commit(action: str, target: str = "", detail: str = "", user_id: int | None = None) -> None:
    record(action, target, detail, user_id)
    db.session.commit()
