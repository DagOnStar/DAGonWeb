"""Dispatch workflow runs to an execution backend.

``thread`` (default)
    Runs execute in background threads of the web process. A per-process
    semaphore caps concurrent runs (``max_concurrent_runs``); extra runs wait
    in ``pending``.
``redis``
    Runs are queued on Redis with RQ and executed by ``flask --app wsgi
    worker`` processes, which may live on other hosts as long as they share
    the database and the scratch volume. Use it for many concurrent or
    long-running workflows: runs survive web restarts and throughput scales
    with the number of workers.

If Redis is selected but unreachable, the run falls back to a thread and the
run log says so.
"""
from __future__ import annotations

import shutil
import threading
from typing import Any

from flask import Flask, current_app

from .. import settings
from ..extensions import db
from ..models import WorkflowRun
from .local import timestamped_log

JOB_FUNCTION = "app.jobs.run_workflow_job"


def redis_connection(url: str | None = None) -> Any:
    from redis import Redis

    return Redis.from_url(url or str(settings.get("redis_url")), socket_connect_timeout=3, socket_timeout=10)


def redis_queue(connection: Any = None) -> Any:
    from rq import Queue

    connection = connection or current_app.extensions.get("dagonweb_redis_override") or redis_connection()
    return Queue(str(settings.get("redis_queue") or "dagonweb"), connection=connection)


def _append_log(run_id: int, message: str) -> None:
    run = db.session.get(WorkflowRun, run_id)
    if run:
        run.log = (run.log or "") + timestamped_log(message)
        db.session.commit()


def submit_run(workflow_id: int, run_id: int, resume: bool = False) -> str:
    """Start or enqueue a run; return the backend that accepted it."""
    run = db.session.get(WorkflowRun, run_id)
    if run is not None and run.unix_username:
        # Cluster runs execute on the cluster; this process only submits and
        # follows them, so they take no local execution slot and need no
        # worker. A restart is harmless: another process reattaches.
        start_cluster_thread(current_app._get_current_object(), workflow_id, run_id, resume)
        return "cluster"
    if settings.get("execution_backend") == "redis":
        try:
            queue = redis_queue()
            queue.enqueue(
                JOB_FUNCTION,
                workflow_id,
                run_id,
                resume,
                job_timeout=int(settings.get("run_timeout_hours")) * 3600,
                result_ttl=86400,
                failure_ttl=7 * 86400,
                description=f"DAGonWeb run {run_id} of workflow {workflow_id}",
            )
        except Exception as exc:  # unreachable Redis must not lose the run
            current_app.logger.warning("Redis dispatch failed, using a thread: %s", exc)
            _append_log(run_id, f"Redis queue unavailable ({exc.__class__.__name__}); running in the web process instead.")
        else:
            _append_log(run_id, f"Queued on Redis queue '{queue.name}'; waiting for a worker.")
            return "redis"
    start_thread(current_app._get_current_object(), workflow_id, run_id, resume)
    return "thread"


def start_cluster_thread(app: Flask, workflow_id: int, run_id: int, resume: bool = False) -> threading.Thread:
    def submit_and_follow() -> None:
        with app.app_context():
            run_job(workflow_id, run_id, resume)

    thread = threading.Thread(target=submit_and_follow, daemon=True, name=f"DAGonWeb-cluster-run-{run_id}")
    thread.start()
    return thread


def _slots(app: Flask) -> threading.BoundedSemaphore:
    limit = int(settings.get("max_concurrent_runs") or 4)
    current = app.extensions.get("dagonweb_run_slots")
    if not current or current[0] != limit:
        current = (limit, threading.BoundedSemaphore(limit))
        app.extensions["dagonweb_run_slots"] = current
    return current[1]


def start_thread(app: Flask, workflow_id: int, run_id: int, resume: bool = False) -> threading.Thread:
    slots = _slots(app)

    def execute_in_background() -> None:
        with app.app_context():
            if not slots.acquire(blocking=False):
                _append_log(run_id, "Waiting for a free execution slot.")
                slots.acquire()
            try:
                run_job(workflow_id, run_id, resume)
            finally:
                slots.release()

    thread = threading.Thread(target=execute_in_background, daemon=False, name=f"DAGonWeb-run-{run_id}")
    thread.start()
    return thread


def run_job(workflow_id: int, run_id: int, resume: bool = False) -> None:
    """Execute one run inside an application context (thread or worker)."""
    from .. import hpc
    from ..models import Workflow
    from .local import execute_workflow

    try:
        workflow = db.session.get(Workflow, workflow_id)
        workflow_run = db.session.get(WorkflowRun, run_id)
        if workflow and workflow_run:
            if workflow_run.unix_username:
                from .remote import execute_remote

                execute_remote(workflow, workflow_run, resume=resume)
            elif hpc.enabled():
                # With cluster execution on, nothing may run as the container user.
                raise RuntimeError("cluster execution is enabled, so runs must execute as a linked cluster account.")
            else:
                execute_workflow(workflow, workflow_run.user_id, workflow_run, resume=resume)
    except Exception as exc:
        db.session.rollback()
        workflow_run = db.session.get(WorkflowRun, run_id)
        if workflow_run:
            workflow_run.status = "failed"
            workflow_run.log = (workflow_run.log or "") + timestamped_log(f"Unable to start DAGonStar workflow: {exc}")
            db.session.commit()


def backend_status() -> dict[str, Any]:
    """Describe the dispatcher and the DAGonStar back-ends visible here."""
    status: dict[str, Any] = {"backend": settings.get("execution_backend"), "redis": None}
    if status["backend"] == "redis":
        try:
            from rq import Worker

            connection = current_app.extensions.get("dagonweb_redis_override") or redis_connection()
            connection.ping()
            queue = redis_queue(connection)
            status["redis"] = {"ok": True, "queued": queue.count, "workers": len(Worker.all(connection=connection)), "queue": queue.name}
        except Exception as exc:
            status["redis"] = {"ok": False, "error": str(exc)}
    tools = {
        "Docker": "docker",
        "Kubernetes (kubectl)": "kubectl",
        "Slurm (sbatch)": "sbatch",
        "Apptainer": "apptainer",
        "Singularity": "singularity",
        "Nomad": "nomad",
    }
    status["tools"] = [{"label": label, "command": command, "path": shutil.which(command)} for label, command in tools.items()]
    return status
