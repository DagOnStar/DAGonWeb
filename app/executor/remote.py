"""Execute runs on the HPC cluster as the submitter's own Unix account.

The web side never builds or runs DAGonStar tasks here. It turns the stored
workflow into a *run specification* — the same ``DagonTask`` arguments the
local executor uses, taken from ``app/task_specs.py`` — and hands it to
``dagonweb-runner`` over SSH (``app/hpc.py``). The runner starts DAGonStar
detached on the login node, inside the user's own run directory, and writes
task events to a JSON-lines file. This module polls those events and keeps
``WorkflowRun`` / ``TaskRun`` updated exactly like the local executor does.
"""
from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Any

from flask import current_app

from .. import hpc, settings
from ..dagon_ini import merge_workflow_dagon_config, runtime_dagon_config
from ..extensions import db
from ..interactive import substitute_runtime
from ..models import LINK_EXPLICIT, RunStatus, TaskRun, Workflow, WorkflowRun
from ..task_specs import INTERACTIVE_TASK_TYPES, dagon_task_arguments, runtime_type
from .local import timestamped_log

# Upload and Form tasks wait for files the run page writes into the run
# directory; that needs a write verb in the runner (phase 2).
UNSUPPORTED_TASK_TYPES = {"upload", "form"}
FINAL_STATES = {"success", "failed", "lost"}


def remote_run_dir(base: str, run_id: int) -> str:
    return str(PurePosixPath(base) / f"run_{run_id}")


def build_run_spec(workflow: Workflow, run_dir: str, python: str) -> dict[str, Any]:
    """The ``dagonweb-runner submit`` document for ``workflow``.

    ``run_dir`` and ``python`` are the cluster-side run directory and
    interpreter reported by ``dagonweb-runner ping``.
    """
    unsupported = sorted(task.name for task in workflow.tasks if task.normalized_task_type in UNSUPPORTED_TASK_TYPES)
    if unsupported:
        raise ValueError(f"Upload and Form tasks cannot run on the cluster yet: {', '.join(unsupported)}.")
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
    return {
        "name": workflow.name,
        "config": config,
        "runtime": {key: options.get(key) for key in ("max_threads", "portable_emulation", "pre_stage") if key in options},
        "tasks": tasks,
        "explicit": explicit,
        "native": native,
    }


def _log(run: WorkflowRun, message: str) -> None:
    run.log = (run.log or "") + timestamped_log(message)


def apply_event(run: WorkflowRun, task_runs: dict[str, TaskRun], event: dict[str, Any]) -> None:
    """Record one runner event with the local executor's log wording."""
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
            _log(run, f"Submitting to {settings.get('hpc_ssh_host')} as {user.unix_username} (dagonweb-runner {info.get('version')}, DAGonStar {info.get('dagonstar') or 'unknown'}).")
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
        _log(run, f"Cluster run failed: {exc}")
        db.session.commit()
    return db.session.get(WorkflowRun, run_id)


def follow(client: hpc.RunnerClient, run_id: int) -> None:
    """Poll ``dagonweb-runner status`` until the run ends or times out."""
    interval = float(settings.get("hpc_poll_seconds") or 5)
    deadline = time.monotonic() + int(settings.get("run_timeout_hours")) * 3600
    offset = 0
    while True:
        status = client.status(run_id, offset)
        offset = int(status.get("offset") or offset)
        db.session.expire_all()
        run = db.session.get(WorkflowRun, run_id)
        task_runs = {task_run.task_uid: task_run for task_run in run.task_runs}
        for event in status.get("events") or []:
            if isinstance(event, dict):
                apply_event(run, task_runs, event)
        state = status.get("state")
        if state in FINAL_STATES and not status.get("more"):
            if state == "lost":
                _log(run, "The DAGonStar process on the cluster stopped without finishing (killed or the node rebooted). Resume the run to continue.")
                run.status = RunStatus.FAILED.value
            else:
                run.status = RunStatus.SUCCESS.value if state == "success" else RunStatus.FAILED.value
            _log(run, f"DAGonStar workflow completed with status: {run.status}.")
            db.session.commit()
            return
        db.session.commit()
        if time.monotonic() > deadline:
            client.cancel(run_id)
            run.status = RunStatus.FAILED.value
            _log(run, "The run exceeded the run timeout and was cancelled on the cluster.")
            db.session.commit()
            return
        if not status.get("more"):
            time.sleep(interval)
