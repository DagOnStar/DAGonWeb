from __future__ import annotations

import json
import importlib
import os
import shutil
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import networkx as nx
from flask import current_app
from ..dagon_ini import runtime_dagon_config
from ..extensions import db
from ..models import LEGACY_TASK_TYPES, RunStatus, Setting, TaskRun, Workflow, WorkflowRun


def timestamped_log(message: str) -> str:
    """Format an execution event for display in the live run log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[{now}] {message}\n"


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


def dagonstar_document(workflow: Workflow) -> dict[str, Any]:
    """Build the runtime-only DAGonStar document for a stored workflow."""
    document = workflow.as_json()
    for task in document["tasks"].values():
        if task["type"] == "web":
            # DAGonStar's JSON loader otherwise defaults to a ``python``
            # executable that may not exist in a virtualenv-only deployment.
            task["python"] = sys.executable
        elif task["type"] == "native" and not task.get("python"):
            # Keep the native runner in the same environment as the web app.
            task["python"] = sys.executable
    return document


def prepare_native_task_environments(workflow: Workflow, root: Path, document: dict[str, Any]) -> None:
    """Install each Native task's declared requirements in its own scratch venv."""
    environment_root = safe_child(root, f"workflow-{workflow.id}", "native_environments")
    for task in workflow.tasks:
        if task.normalized_task_type != "native":
            continue
        requirements = task.config.get("requirements", "")
        if not isinstance(requirements, str) or not requirements.strip():
            continue
        if task.config.get("executor", "local") != "local":
            raise ValueError(f"Native task {task.name} requirements.txt is supported only by the local executor.")
        if "\0" in requirements or len(requirements) > 50_000:
            raise ValueError(f"Native task {task.name} has invalid requirements.txt content.")
        task_root = safe_child(environment_root, task.name)
        task_root.mkdir(parents=True, exist_ok=True)
        requirements_path = safe_child(task_root, "requirements.txt")
        requirements_path.write_text(requirements.rstrip() + "\n", encoding="utf-8")
        virtualenv_root = safe_child(task_root, "venv")
        python = virtualenv_root / "bin" / "python"
        if not python.is_file():
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(virtualenv_root)
        completed = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(requirements_path)],
            text=True,
            capture_output=True,
        )
        if completed.returncode:
            output = (completed.stdout + completed.stderr).strip()
            raise RuntimeError(f"Native task {task.name} requirements installation failed.\n{output}")
        document["tasks"][task.name]["python"] = str(python)


def materialize_native_sources(workflow: Workflow, root: Path) -> tuple[Path | None, set[str]]:
    """Write workflow-owned Native sources under scratch so they are importable.

    Sources are never accepted as filesystem paths: the module name decides a
    safe relative destination below the configured scratch root.
    """
    module_root = safe_child(root, f"workflow-{workflow.id}", "native_modules")
    modules: set[str] = set()
    for task in workflow.tasks:
        config = task.config
        source = config.get("source")
        if task.normalized_task_type != "native" or not isinstance(source, str) or not source.strip():
            continue
        function = config.get("callable", "")
        module, separator, _ = function.partition(":")
        parts = module.split(".")
        if not separator or not parts or any(not part.isidentifier() for part in parts):
            raise ValueError("Native source requires a callable in module:function form.")
        try:
            compile(source, f"{module}.py", "exec")
        except SyntaxError as exc:
            raise ValueError(f"Native source has invalid Python syntax: {exc.msg}") from exc
        target = safe_child(module_root, *parts[:-1], f"{parts[-1]}.py")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.rstrip() + "\n", encoding="utf-8")
        parent = module_root
        for part in parts[:-1]:
            parent = parent / part
            (parent / "__init__.py").touch(exist_ok=True)
        modules.add(module)
    return (module_root, modules) if modules else (None, modules)


def execute_workflow(workflow: Workflow, user_id: int, run: WorkflowRun | None = None) -> WorkflowRun:
    root = scratch_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = safe_child(root, f"workflow-{workflow.id}", f"run-{stamp}" if run is None else f"run-{run.id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    if run is None:
        run = WorkflowRun(workflow_id=workflow.id, user_id=user_id, status=RunStatus.RUNNING.value, scratch_path=str(run_dir), log="")
        db.session.add(run)
    else:
        run.status = RunStatus.RUNNING.value
        run.scratch_path = str(run_dir)
    run.log = (run.log or "") + timestamped_log("Workflow started.")
    db.session.commit()
    module_root: Path | None = None
    try:
        from dagon import Workflow as DagonStarWorkflow

        module_root, modules = materialize_native_sources(workflow, root)
        if module_root:
            sys.path.insert(0, str(module_root))
            importlib.invalidate_caches()
            for module in modules:
                sys.modules.pop(module, None)

        runtime_document = dagonstar_document(workflow)
        prepare_native_task_environments(workflow, root, runtime_document)
        runtime = DagonStarWorkflow(
            workflow.name,
            config=runtime_dagon_config(Path(current_app.config["DAGON_INI_PATH"]), root),
            jsonload=runtime_document,
        )
        if module_root:
            sys.path.remove(str(module_root))
        task_runs = {task.name: TaskRun(workflow_run_id=run.id, task_uid=task.name, status=RunStatus.PENDING.value, scratch_path=str(run_dir), log="") for task in runtime.tasks}
        db.session.add_all(task_runs.values())
        db.session.commit()
        app = current_app._get_current_object()

        def update_task_status(task: Any) -> None:
            with app.app_context():
                task_run = db.session.get(TaskRun, task_runs[task.name].id)
                if task_run:
                    status = {"RUNNING": RunStatus.RUNNING.value, "FINISHED": RunStatus.SUCCESS.value, "FAILED": RunStatus.FAILED.value}.get(task.status.name, RunStatus.PENDING.value)
                    previous_status = task_run.status
                    task_run.status = status
                    task_run.scratch_path = task.working_dir or str(run_dir)
                    if status != previous_status:
                        event = {RunStatus.RUNNING.value: "started", RunStatus.SUCCESS.value: "completed", RunStatus.FAILED.value: "failed"}.get(status)
                        if event:
                            message = timestamped_log(f"Task {task.name} {event}.")
                            task_run.log = (task_run.log or "") + message
                            current_run = db.session.get(WorkflowRun, run.id)
                            if current_run:
                                current_run.log = (current_run.log or "") + message
                    db.session.commit()

        runtime.on_task_start += update_task_status
        runtime.on_task_end += update_task_status
        runtime.launch()
        runtime.wait()
        for runtime_task in runtime.tasks:
            update_task_status(runtime_task)
        run.status = RunStatus.SUCCESS.value if all(task.status.name == "FINISHED" for task in runtime.tasks) else RunStatus.FAILED.value
        run.log = (run.log or "") + timestamped_log(f"DAGonStar workflow completed with status: {run.status}.")
    except Exception as exc:
        if module_root and str(module_root) in sys.path:
            sys.path.remove(str(module_root))
        run.status = RunStatus.FAILED.value
        run.log = (run.log or "") + timestamped_log(f"DAGonStar workflow failed: {exc}")
    db.session.commit()
    return run

    try:
        graph = nx.DiGraph()
        task_by_uid = {task.name: task for task in workflow.tasks}
        for task in workflow.tasks:
            graph.add_node(task.name)
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
    task_type = LEGACY_TASK_TYPES.get(task.task_type, task.task_type)
    context = {"TASK_NAME": task.name, "TASK_SCRATCH": str(task_dir), "WORKFLOW_ID": str(workflow.id)}
    for link in workflow.links:
        if link.target_uid == task.name:
            context[f"INPUT_{link.target_input.upper()}"] = link.workflow_uri
    env = os.environ.copy()
    env.update(context)
    metadata = {"task": task.to_dict(), "env": context}
    (task_dir / "task.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if task_type == "checkpoint":
        (task_dir / "checkpoint.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return "Checkpoint metadata written."
    if task_type == "batch":
        command = config.get("command", "echo hello from DAGonWeb > output.txt")
        completed = subprocess.run(command, shell=True, cwd=task_dir, env=env, text=True, capture_output=True, timeout=int(config.get("timeout", 3600)))
        output = completed.stdout + completed.stderr
        if completed.returncode:
            raise RuntimeError(f"Batch task exited with status {completed.returncode}.\n{output}")
        return output
    if task_type in {"slurm", "cloud", "docker"}:
        (task_dir / f"{task_type}_job.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return f"{task_type.title()} job metadata staged. Configure an execution adapter to submit it."
    if task_type == "native":
        module_function = config.get("callable", "")
        (task_dir / "native_result.json").write_text(json.dumps({"callable": module_function, "status": "stubbed"}, indent=2), encoding="utf-8")
        return "Native DAGonStar-style task metadata written. Install a project-specific callable adapter to execute it."
    if task_type == "llm":
        (task_dir / "prompt.json").write_text(json.dumps({"prompt": config.get("prompt", "")}, indent=2), encoding="utf-8")
        return "LLM task prompt staged. Configure an OpenAI-compatible backend before production execution."
    if task_type == "web":
        (task_dir / "web_request.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        return "Web request metadata staged. Configure a web execution adapter before production execution."
    raise RuntimeError(f"Unsupported task type {task_type}")


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


def preview_task_file(path: Path) -> dict[str, Any]:
    """Return a safe, presentation-neutral preview payload for a task artifact."""
    if _is_netcdf(path):
        return {"kind": "netcdf", "metadata": read_netcdf_metadata(path)}
    content = read_text_file(path)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        return {"kind": "geojson" if is_geojson(value) else "json", "content": content}
    if path.suffix.lower() in {".csv", ".tsv"}:
        return {"kind": "csv", "content": content, "delimiter": "\t" if path.suffix.lower() == ".tsv" else ","}
    return {"kind": "text", "content": content}


def is_geojson(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in {"Feature", "FeatureCollection", "GeometryCollection", "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


def _is_netcdf(path: Path) -> bool:
    if path.suffix.lower() in {".nc", ".nc4", ".cdf", ".netcdf"}:
        return True
    if not path.is_file():
        return False
    with path.open("rb") as file:
        signature = file.read(8)
    return signature.startswith(b"CDF") or signature == b"\x89HDF\r\n\x1a\n"


def read_netcdf_metadata(path: Path, max_bytes: int = 100_000_000) -> dict[str, Any]:
    """Read descriptive metadata only; NetCDF array values are never loaded."""
    if not path.is_file():
        raise ValueError("Path is not a file")
    if path.stat().st_size > max_bytes:
        raise ValueError("NetCDF file is too large to preview")
    try:
        from netCDF4 import Dataset
    except ImportError as exc:
        raise ValueError("NetCDF preview support is unavailable") from exc
    try:
        with Dataset(path, "r") as dataset:
            return {
                "dimensions": {name: {"size": len(dimension), "unlimited": dimension.isunlimited()} for name, dimension in dataset.dimensions.items()},
                "variables": {name: {"dimensions": list(variable.dimensions), "shape": list(variable.shape), "dtype": str(variable.dtype), "attributes": {attribute: _metadata_value(variable.getncattr(attribute)) for attribute in variable.ncattrs()}} for name, variable in dataset.variables.items()},
                "attributes": {attribute: _metadata_value(dataset.getncattr(attribute)) for attribute in dataset.ncattrs()},
            }
    except (OSError, RuntimeError) as exc:
        raise ValueError("Unable to read NetCDF metadata") from exc


def _metadata_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return value[:100]
    return str(value)
