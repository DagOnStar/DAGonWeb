"""Entry points executed by RQ workers (``flask --app wsgi worker``)."""
from __future__ import annotations


def run_workflow_job(workflow_id: int, run_id: int, resume: bool = False) -> None:
    """Execute a queued run in a fresh application context."""
    from . import create_app
    from .executor.dispatch import run_job

    app = create_app()
    with app.app_context():
        run_job(workflow_id, run_id, resume)
