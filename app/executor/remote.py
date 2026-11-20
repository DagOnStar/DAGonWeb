"""Execute runs on the HPC cluster as the submitter's own Unix account.

The web side never builds or runs DAGonStar tasks here. It turns the stored
workflow into a *run specification* — the same ``DagonTask`` arguments the
local executor uses, taken from ``app/task_specs.py`` — and hands it to
``dagonweb-runner`` over SSH (``app/hpc.py``). The runner starts DAGonStar
inside the user's own run directory, detached on the login node or as a
small Slurm job, and writes task events to a JSON-lines file. This module
polls those events and keeps ``WorkflowRun`` / ``TaskRun`` updated exactly
like the local executor does.

Following a run is cheap and restartable: the event offset and a heartbeat
are stored on the run, so when the web process restarts, the next process
that sees the run (its page, its status, or ``flask --app wsgi hpc-reattach``)
claims it and carries on from where the previous one stopped.
"""
from __future__ import annotations

import json
import shlex
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import sqlalchemy as sa
from flask import Flask, current_app

from .. import hpc, settings
from ..dagon_ini import merge_workflow_dagon_config, runtime_dagon_config
from ..extensions import db
from ..interactive import safe_filename, substitute_runtime
from ..models import LINK_EXPLICIT, RunStatus, TaskRun, Workflow, WorkflowRun
from ..task_specs import INTERACTIVE_TASK_TYPES, dagon_task_arguments, runtime_type
from .local import timestamped_log

FINAL_STATES = {"success", "failed", "lost"}
RESULT_TASK_TYPES = ("table", "chart", "download")
MAX_RESULT_BYTES = 2_000_000
MAX_POLL_FAILURES = 10


def remote_run_dir(base: str, run_id: int) -> str:
    return str(PurePosixPath(base) / f"run_{run_id}")


def utcnow() -> datetime:
    """Naive UTC, as every database back-end stores it the same way."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def orchestrator_options() -> dict[str, Any]:
    options = shlex.split(str(settings.get("hpc_sbatch_options") or ""))
    return {"mode": settings.get("hpc_orchestrator"), "sbatch_options": options}


def fair_spec(workflow: Workflow) -> dict[str, Any] | None:
    profile = workflow.fair_profile
    if not profile.get("enabled"):
        return None
    return {
        "enabled": True,
        "title": profile.get("title") or workflow.name,
        "description": profile.get("description") or workflow.description or "",
        "creators": [creator["name"] for creator in profile.get("creators", []) if isinstance(creator, dict) and creator.get("name")],
        "license": profile.get("license") or "",
        "keywords": list(profile.get("keywords") or []),
        "access_policy": profile.get("access_policy") or "local",
        "strict": bool(profile.get("strict", False)),
        "capture_environment": bool(profile.get("capture_environment")),
        "environment_allowlist": list(profile.get("environment_allowlist") or []),
    }


def build_run_spec(workflow: Workflow, run_dir: str, python: str) -> dict[str, Any]:
    """The ``dagonweb-runner submit`` document for ``workflow``.

    ``run_dir`` and ``python`` are the cluster-side run directory and
    interpreter reported by ``dagonweb-runner ping``.
    """
    base = runtime_dagon_config(Path(current_app.config["DAGON_INI_PATH"]), Path(run_dir))
    config = merge_workflow_dagon_config(base, workflow.dagon_ini)
    config.setdefault("batch", {}).update({"scratch_dir_base": run_dir, "remove_dir": False})
    data_dir = str(PurePosixPath(run_dir) / "_dagonweb" / "data")
    tasks, native = [], []
    for task in workflow.tasks:
        task_type = task.normalized_task_type
        args, kwargs = dagon_task_arguments(task_type, task.name, task.config)
        if task_type == "web" and not task.config.get("python"):
            kwargs["python"] = python
        if task_type in INTERACTIVE_TASK_TYPES:
            args[1] = substitute_runtime(args[1], data_dir, python)
        if task_type == "native":
            item: dict[str, Any] = {"task": task.name}
            source = task.config.get("source")
            if isinstance(source, str) and source.strip():
                module, separator, _ = str(task.config.get("callable", "")).partition(":")
                if not separator or not all(part.isidentifier() for part in module.split(".")):
                    raise ValueError("Native source requires a callable in module:function form.")
                try:
                    compile(source, f"{module}.py", "exec")
                except SyntaxError as exc:
                    raise ValueError(f"Native source has invalid Python syntax: {exc.msg}") from exc
                item.update(module=module, source=source)
            requirements = task.config.get("requirements")
            if isinstance(requirements, str) and requirements.strip():
                if task.config.get("executor", "local") != "local":
                    raise ValueError(f"Native task {task.name} requirements.txt is supported only by the local executor.")
                if "\0" in requirements or len(requirements) > 50_000:
                    raise ValueError(f"Native task {task.name} has invalid requirements.txt content.")
                item["requirements"] = requirements
            if len(item) > 1:
                native.append(item)
        tasks.append({"name": task.name, "type": runtime_type(task_type), "args": args, "kwargs": kwargs})
    names = {task["name"] for task in tasks}
    explicit = [[link.target_uid, link.source_uid] for link in workflow.links if link.kind == LINK_EXPLICIT and {link.target_uid, link.source_uid} <= names]
    options = workflow.runtime_options
    spec = {
        "name": workflow.name,
        "config": config,
        "runtime": {key: options.get(key) for key in ("max_threads", "portable_emulation", "pre_stage") if key in options},
        "tasks": tasks,
        "explicit": explicit,
        "native": native,
        "orchestrator": orchestrator_options(),
    }
    fair = fair_spec(workflow)
    if fair:
        spec["fair"] = fair
    return spec


def _log(run: WorkflowRun, message: str) -> None:
    run.log = (run.log or "") + timestamped_log(message)


def apply_event(run: WorkflowRun, task_runs: dict[str, TaskRun], event: dict[str, Any]) -> None:
    """Record one runner event with the local executor's log wording."""
    if event.get("run") == "queued":
        _log(run, f"Waiting in the Slurm queue as job {event.get('job_id')}.")
        run.update_remote_meta(job_id=event.get("job_id"))
        return
    if event.get("run") == "started":
        _log(run, "DAGonStar resumed from checkpoint on the cluster." if event.get("resumed") else "DAGonStar started on the cluster.")
        return
    if event.get("run") == "finished":
        if event.get("error"):
            _log(run, f"DAGonStar workflow failed: {event['error']}")
        return
    task_run = task_runs.get(str(event.get("task") or ""))
    if task_run is None:
        return
    status = event.get("status") if event.get("status") in {"pending", "running", "success", "failed"} else task_run.status
    messages = []
    if event.get("event"):
        messages.append(f"Task {task_run.task_uid}: {event['event']}.")
    if status != task_run.status:
        verb = {"running": "started", "success": "completed", "failed": "failed"}.get(status)
        if verb:
            messages.append(f"Task {task_run.task_uid} {verb}.")
        if status == "failed" and event.get("error"):
            messages.append(f"Task {task_run.task_uid} error: {event['error']}")
    task_run.status = status
    working_dir = str(event.get("working_dir") or "")
    if working_dir and PurePosixPath(working_dir).is_relative_to(PurePosixPath(run.scratch_path)):
        task_run.scratch_path = working_dir
    for message in messages:
        line = timestamped_log(message)
        task_run.log = (task_run.log or "") + line
        run.log = (run.log or "") + line


def execute_remote(workflow: Workflow, run: WorkflowRun, *, resume: bool = False) -> WorkflowRun:
    """Submit ``run`` to the cluster as its submitter and follow it to the end."""
    run_id = run.id
    user = run.user
    try:
        if not user or not user.unix_linked or user.unix_username != run.unix_username:
            raise hpc.HpcError("The run's cluster account is no longer linked to its submitter.")
        with hpc.client_for(user) as client:
            info = client.ping()
            run_dir = remote_run_dir(str(info["base"]), run.id)
            spec = build_run_spec(workflow, run_dir, str(info.get("python") or "python3"))
            run.status = RunStatus.RUNNING.value
            run.scratch_path = run_dir
            # A new attempt reads events from the start and takes new inputs.
            run.remote_meta = {"offset": 0, "inputs": [], "results": {}}
            run.monitor_heartbeat = utcnow()
            mode = spec["orchestrator"]["mode"]
            _log(run, f"Submitting to {settings.get('hpc_ssh_host')} as {user.unix_username} (dagonweb-runner {info.get('version')}, DAGonStar {info.get('dagonstar') or 'unknown'}, orchestrator on {'Slurm' if mode == 'slurm' else 'the login node'}).")
            for task_run in list(run.task_runs):
                db.session.delete(task_run)
            db.session.flush()
            for task in spec["tasks"]:
                db.session.add(TaskRun(workflow_run_id=run_id, task_uid=task["name"], status=RunStatus.PENDING.value, scratch_path=run_dir, log=""))
            db.session.commit()
            client.submit(run.id, spec, resume=resume)
            follow(client, run_id)
    except Exception as exc:  # noqa: BLE001 - every failure ends up in the run log
        db.session.rollback()
        run = db.session.get(WorkflowRun, run_id)
        run.status = RunStatus.FAILED.value
        run.monitor_heartbeat = None
        _log(run, f"Cluster run failed: {exc}")
        db.session.commit()
    return db.session.get(WorkflowRun, run_id)


def locked_run(run_id: int) -> WorkflowRun:
    """Lock the run row, then re-read it, right before changing it.

    The follower and the run page both write this row (inputs, log lines), so
    every change is lock-read-modify-commit with no network call in between.
    The no-op UPDATE takes the write lock first: a row lock on PostgreSQL and
    MySQL, and the database write lock on SQLite, which ignores ``FOR UPDATE``.
    The lock is released by the caller's commit or rollback.
    """
    db.session.expire_all()
    db.session.execute(
        sa.update(WorkflowRun).where(WorkflowRun.id == run_id).values(id=WorkflowRun.id).execution_options(synchronize_session=False)
    )
    return db.session.query(WorkflowRun).filter(WorkflowRun.id == run_id).with_for_update().one()


def follow(client: hpc.RunnerClient, run_id: int) -> None:
    """Poll ``dagonweb-runner status`` until the run ends.

    Network trouble does not fail the run: after a few failed polls this
    process stops following, and the heartbeat goes stale so another one
    reconnects later.
    """
    interval = float(settings.get("hpc_poll_seconds") or 5)
    deadline_hours = int(settings.get("run_timeout_hours"))
    failures = 0
    while True:
        db.session.expire_all()
        run = db.session.get(WorkflowRun, run_id)
        if run is None or run.status != RunStatus.RUNNING.value:
            return
        try:
            status = client.status(run_id, int(run.remote_meta.get("offset") or 0))
        except hpc.HpcError as exc:
            failures += 1
            run = locked_run(run_id)
            if failures == 1:
                _log(run, f"Lost contact with the cluster ({exc}); retrying.")
            if failures >= MAX_POLL_FAILURES:
                _log(run, "Stopped following the run; DAGonWeb reconnects when the run page is opened again.")
                run.monitor_heartbeat = None
                db.session.commit()
                return
            db.session.commit()
            time.sleep(min(interval * failures, 60))
            continue

        run = locked_run(run_id)
        if run.status != RunStatus.RUNNING.value:
            db.session.commit()
            return
        if failures:
            _log(run, "Contact with the cluster restored.")
            failures = 0
        task_runs = {task_run.task_uid: task_run for task_run in run.task_runs}
        for event in status.get("events") or []:
            if isinstance(event, dict):
                apply_event(run, task_runs, event)
        run.update_remote_meta(offset=int(status.get("offset") or 0))
        run.monitor_heartbeat = utcnow()
        wanted = results_to_fetch(run, task_runs)
        db.session.commit()

        if wanted:
            fetched = fetch_results(client, run_id, wanted)
            run = locked_run(run_id)
            run.update_remote_meta(results={**(run.remote_meta.get("results") or {}), **fetched})
            db.session.commit()

        state = status.get("state")
        if state in FINAL_STATES and not status.get("more"):
            fair, fair_error = fetch_fair(client, run)
            run = locked_run(run_id)
            if state == "lost":
                _log(run, "The DAGonStar process on the cluster stopped without finishing (killed, cancelled or the node rebooted). Resume the run to continue.")
                run.status = RunStatus.FAILED.value
            else:
                run.status = RunStatus.SUCCESS.value if state == "success" else RunStatus.FAILED.value
            if fair:
                run.update_remote_meta(fair=fair)
            if fair_error:
                _log(run, f"Could not read the FAIR exports from the cluster: {fair_error}")
            _log(run, f"DAGonStar workflow completed with status: {run.status}.")
            run.monitor_heartbeat = None
            db.session.commit()
            return
        if run.created_at and utcnow() - run.created_at.replace(tzinfo=None) > timedelta(hours=deadline_hours):
            client.cancel(run_id)
            run = locked_run(run_id)
            run.status = RunStatus.FAILED.value
            run.monitor_heartbeat = None
            _log(run, "The run exceeded the run timeout and was cancelled on the cluster.")
            db.session.commit()
            return
        if not status.get("more"):
            time.sleep(interval)


# ---------------------------------------------------------------- results and FAIR


def results_to_fetch(run: WorkflowRun, task_runs: dict[str, TaskRun]) -> list[dict[str, Any]]:
    """Finished Table/Chart/Download tasks whose results were not fetched yet."""
    known = run.remote_meta.get("results") or {}
    wanted = []
    for task in run.workflow.tasks:
        kind = task.normalized_task_type
        task_run = task_runs.get(task.name)
        if kind not in RESULT_TASK_TYPES or task.name in known or not task_run or task_run.status != RunStatus.SUCCESS.value:
            continue
        if task_run.scratch_path == run.scratch_path:
            continue  # the event with the task folder has not arrived yet
        wanted.append({
            "task": task.name, "kind": kind,
            "folder": str(PurePosixPath(task_run.scratch_path).relative_to(PurePosixPath(run.scratch_path))),
            "archive": safe_filename(task.config.get("archive"), "results.zip"),
        })
    return wanted


def fetch_results(client: hpc.RunnerClient, run_id: int, wanted: list[dict[str, Any]]) -> dict[str, Any]:
    """Read the result files of ``wanted`` over SSH (no database access)."""
    fetched: dict[str, Any] = {}
    for want in wanted:
        item: dict[str, Any] = {"kind": want["kind"]}
        try:
            if want["kind"] == "download":
                files = {entry["name"]: entry for entry in client.ls(run_id, f"{want['folder']}/outputs")}
                if want["archive"] in files:
                    item.update(name=want["archive"], size=files[want["archive"]]["size"], path=f"outputs/{want['archive']}")
            else:
                item["data"] = json.loads(client.get(run_id, f"{want['folder']}/outputs/{want['kind']}.json", MAX_RESULT_BYTES).decode("utf-8"))
        except (hpc.HpcError, ValueError, UnicodeDecodeError):
            item["missing"] = True
        fetched[want["task"]] = item
    return fetched


def fetch_fair(client: hpc.RunnerClient, run: WorkflowRun) -> tuple[dict[str, Any] | None, str]:
    """List the FAIR exports DAGonStar wrote on the cluster, and read the report."""
    from ..workflows.routes import FAIR_EXPORT_FILES

    if not run.workflow.fair_profile.get("enabled"):
        return None, ""
    run_id, base = run.id, f".dagon/fair/{run.workflow.name}"
    try:
        candidates = [base]
        entries = client.ls(run_id, base)
        if "run.json" not in {entry["name"] for entry in entries}:
            folders = sorted((entry for entry in entries if entry["is_dir"]), key=lambda entry: entry["modified_at"], reverse=True)
            candidates = [f"{base}/{entry['name']}" for entry in folders]
        for folder in candidates:
            listing = {entry["name"]: entry for entry in client.ls(run_id, folder) if not entry["is_dir"]}
            if "run.json" not in listing:
                continue
            files = [{"name": name, "label": label, "size": listing[name]["size"]} for name, label in FAIR_EXPORT_FILES.items() if name in listing]
            report = None
            if "fairness-report.json" in listing:
                try:
                    report = json.loads(client.get(run_id, f"{folder}/fairness-report.json", MAX_RESULT_BYTES).decode("utf-8"))
                except (hpc.HpcError, ValueError, UnicodeDecodeError):
                    report = None
            return {"root": folder, "files": files, "report": report}, ""
        return None, "no run.json was written"
    except hpc.HpcError as exc:
        return None, str(exc)


# ---------------------------------------------------------------- reconnecting


def stale_after() -> timedelta:
    return timedelta(seconds=max(60.0, 6 * float(settings.get("hpc_poll_seconds") or 5)))


def claim(run_id: int) -> bool:
    """Atomically take over following ``run_id`` if nobody has for a while.

    The conditional UPDATE makes this safe across web processes and workers:
    only one of them sees ``rowcount == 1``.
    """
    now = utcnow()
    result = db.session.execute(
        sa.update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.unix_username.isnot(None),
            WorkflowRun.status == RunStatus.RUNNING.value,
            WorkflowRun.scratch_path != "",
            sa.or_(WorkflowRun.monitor_heartbeat.is_(None), WorkflowRun.monitor_heartbeat < now - stale_after()),
        )
        .values(monitor_heartbeat=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def reattach(run_id: int) -> None:
    """Follow a cluster run that an earlier process stopped following."""
    run = db.session.get(WorkflowRun, run_id)
    if run is None:
        return
    user = run.user
    if not user or not user.unix_linked or user.unix_username != run.unix_username:
        run.status = RunStatus.FAILED.value
        run.monitor_heartbeat = None
        _log(run, "Cannot reconnect: the run's cluster account is no longer linked to its submitter.")
        db.session.commit()
        return
    try:
        with hpc.client_for(user) as client:
            run = locked_run(run_id)
            if run.remote_meta.get("reconnect_error"):
                run.update_remote_meta(reconnect_error="")
            _log(run, "Reconnected to the run on the cluster.")
            db.session.commit()
            follow(client, run_id)
    except hpc.HpcError as exc:
        db.session.rollback()
        run = locked_run(run_id)
        if run.remote_meta.get("reconnect_error") != str(exc):
            run.update_remote_meta(reconnect_error=str(exc))
            _log(run, f"Cannot reach the cluster yet ({exc}); DAGonWeb retries later.")
        db.session.commit()


def start_follow_thread(app: Flask, run_id: int, daemon: bool = True) -> threading.Thread:
    def target() -> None:
        with app.app_context():
            reattach(run_id)

    thread = threading.Thread(target=target, daemon=daemon, name=f"DAGonWeb-follow-{run_id}")
    thread.start()
    return thread


def ensure_followed(run: WorkflowRun) -> bool:
    """Make sure some process follows an active cluster run; return True if this one took it."""
    if not run.unix_username or run.status != RunStatus.RUNNING.value or not run.scratch_path:
        return False
    heartbeat = run.monitor_heartbeat
    if heartbeat is not None and utcnow() - heartbeat < stale_after():
        return False
    if not claim(run.id):
        return False
    start_follow_thread(current_app._get_current_object(), run.id)
    return True


def reattach_all(daemon: bool = True) -> list[threading.Thread]:
    """Claim and follow every stale active cluster run (CLI, start-up)."""
    candidates = [run_id for (run_id,) in db.session.query(WorkflowRun.id).filter(WorkflowRun.unix_username.isnot(None), WorkflowRun.status == RunStatus.RUNNING.value)]
    app = current_app._get_current_object()
    return [start_follow_thread(app, run_id, daemon) for run_id in candidates if claim(run_id)]
