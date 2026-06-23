from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import networkx as nx
from flask import current_app
from ..extensions import db
from ..models import RunStatus, Setting, TaskRun, Workflow, WorkflowRun


def scratch_root() -> Path:
    configured = Setting.query.filter_by(key="scratch_dir").first()
    root = Path(configured.value if configured else current_app.config["SCRATCH_DIR"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_child(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Unsafe scratch path")
    return path


def execute_workflow(workflow: Workflow, user_id: int) -> WorkflowRun:
    root = scratch_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = safe_child(root, f"workflow-{workflow.id}", f"run-{stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)
    run = WorkflowRun(workflow_id=workflow.id, user_id=user_id, status=RunStatus.RUNNING.value, scratch_path=str(run_dir), log="")
    db.session.add(run)
    db.session.commit()
    try:
        graph = nx.DiGraph()
        task_by_uid = {task.uid: task for task in workflow.tasks}
        for task in workflow.tasks:
            graph.add_node(task.uid)
        for link in workflow.links:
            graph.add_edge(link.source_uid, link.target_uid)
        if not nx.is_directed_acyclic_graph(graph):
            raise RuntimeError("Workflow is not a DAG")
        for uid in nx.topological_sort(graph):
            task = task_by_uid[uid]
            task_dir = safe_child(run_dir, uid)
            task_dir.mkdir(parents=True, exist_ok=True)
            task_run = TaskRun(workflow_run_id=run.id, task_uid=uid, status=RunStatus.RUNNING.value, scratch_path=str(task_dir), log="")
            db.session.add(task_run)
            db.session.commit()
            try:
                result = execute_task(task, task_dir, workflow)
                task_run.status = RunStatus.SUCCESS.value
                task_run.log = result
            except Exception as exc:
                task_run.status = RunStatus.FAILED.value
                task_run.log = str(exc)
                raise
            finally:
                db.session.commit()
        run.status = RunStatus.SUCCESS.value
        run.log += "Workflow completed successfully.\n"
    except Exception as exc:
        run.status = RunStatus.FAILED.value
        run.log += f"Workflow failed: {exc}\n"
    db.session.commit()
    return run


def execute_task(task: Any, task_dir: Path, workflow: Workflow) -> str:
    config = task.config
    context = {"TASK_UID": task.uid, "TASK_SCRATCH": str(task_dir), "WORKFLOW_ID": str(workflow.id)}
    for link in workflow.links:
        if link.target_uid == task.uid:
            context[f"INPUT_{link.target_input.upper()}"] = link.workflow_uri
    env = os.environ.copy()
    env.update(context)
    metadata = {"task": task.to_dict(), "env": context}
    (task_dir / "task.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if task.task_type == "checkpoint":
        (task_dir / "checkpoint.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return "Checkpoint metadata written."
    if task.task_type == "batch":
        command = config.get("command", "echo hello from DAGonWeb > output.txt")
        completed = subprocess.run(command, shell=True, cwd=task_dir, env=env, text=True, capture_output=True, timeout=int(config.get("timeout", 3600)))
        output = completed.stdout + completed.stderr
        if completed.returncode:
            raise RuntimeError(f"Batch task exited with status {completed.returncode}.\n{output}")
        return output
    if task.task_type in {"slurm", "cloud", "docker"}:
        (task_dir / f"{task.task_type}_job.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return f"{task.task_type.title()} job metadata staged. Configure an execution adapter to submit it."
    if task.task_type == "native":
        module_function = config.get("callable", "")
        (task_dir / "native_result.json").write_text(json.dumps({"callable": module_function, "status": "stubbed"}, indent=2), encoding="utf-8")
        return "Native DAGonStar-style task metadata written. Install a project-specific callable adapter to execute it."
    if task.task_type == "llm":
        (task_dir / "prompt.json").write_text(json.dumps({"prompt": config.get("prompt", "")}, indent=2), encoding="utf-8")
        return "LLM task prompt staged. Configure an OpenAI-compatible backend before production execution."
    if task.task_type == "web":
        (task_dir / "web_request.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return "Web request metadata staged. Configure a web execution adapter before production execution."
    raise RuntimeError(f"Unsupported task type {task.task_type}")


def list_files(path: str) -> list[dict[str, Any]]:
    root = scratch_root()
    base = Path(path).resolve()
    if root not in base.parents and base != root:
        raise ValueError("Unsafe scratch path")
    return list_directory(base)


def list_directory(base: Path) -> list[dict[str, Any]]:
    if not base.exists():
        return []
    if not base.is_dir():
        raise ValueError("Path is not a directory")
    items = []
    for child in sorted(base.iterdir()):
        items.append({"name": child.name, "is_dir": child.is_dir(), "size": child.stat().st_size, "modified_at": child.stat().st_mtime})
    return items


def read_text_file(path: Path, max_bytes: int = 1_000_000) -> str:
    if not path.is_file():
        raise ValueError("Path is not a file")
    if path.stat().st_size > max_bytes:
        raise ValueError("File is too large to preview")
    content = path.read_bytes()
    if b"\0" in content:
        raise ValueError("Binary files cannot be previewed")
    return content.decode("utf-8")
