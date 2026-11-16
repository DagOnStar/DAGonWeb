"""Housekeeping tasks shared by the CLI and the administration panel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .extensions import db
from .models import ACTIVE_RUN_STATUSES, WorkflowRun


def cleanup_runs(days: int) -> int:
    """Delete finished runs (and their scratch) older than ``days`` days."""
    from .workflows.routes import remove_run_scratch

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    runs = WorkflowRun.query.filter(WorkflowRun.updated_at < cutoff, WorkflowRun.status.notin_(ACTIVE_RUN_STATUSES)).all()
    for run in runs:
        remove_run_scratch(run)
        db.session.delete(run)
    db.session.commit()
    return len(runs)
