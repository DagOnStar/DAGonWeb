"""Run DAGonWeb workflows with the DAGonStar runtime.

The executor builds a ``dagon.Workflow`` directly with ``DagonTask(...)``
objects (so every task-type option reaches DAGonStar), records progress
through DAGonStar's event hooks, and runs it with ``launch()`` / ``wait()``.
Every file a run produces stays below the configured scratch root.
"""
from __future__ import annotations

import importlib
import json
import logging
import subprocess
import sys
import tempfile
import threading
import venv
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from flask import current_app
from ..dagon_ini import merge_workflow_dagon_config, runtime_dagon_config
from ..extensions import db
from ..models import LINK_EXPLICIT, RunStatus, Setting, TaskRun, Workflow, WorkflowRun
from ..interactive import substitute_runtime
from ..task_specs import INTERACTIVE_TASK_TYPES, dagon_task_arguments, meta_workflow_sources, runtime_type

CHECKPOINT_FILE = ("_dagonweb", "checkpoint.json")
# User-provided data of interactive Upload/Form tasks, one folder per task.
INTERACTIVE_DATA_DIR = ("_dagonweb", "data")
# Inline workflow definitions of Meta tasks, written below the run directory.
META_WORKFLOW_ROOT = "_dagonweb"
# sys.path is process-global: serialize the short windows that change it.
_IMPORT_LOCK = threading.RLock()
_TASK_EVENTS = {
    "on_task_staging_in_start": "staging inputs",
    "on_task_execute_start": "executing",
    "on_task_staging_out_start": "staging outputs",
}


def timestamped_log(message: str) -> str:
    """Format an execution event for display in the live run log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[{now}] {message}\n"


def scratch_root() -> Path:
    from .. import settings

    root = Path(str(settings.get("scratch_dir") or current_app.config["SCRATCH_DIR"])).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_child(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Unsafe scratch path")
    return path


def is_inside(root: Path, path: Path) -> bool:
    resolved = path.resolve()
    return resolved == root or root in resolved.parents


def enable_fair_if_configured(runtime: Any, workflow: Workflow, run_dir: Path) -> None:
    profile = workflow.fair_profile
    if not profile.get("enabled"):
        return
    try:
        from dagon.fair import AccessPolicy, Agent, FairProfile
    except ImportError as exc:
        raise RuntimeError("The installed DAGonStar package does not provide dagon.fair.") from exc

    creators = []
    for creator in profile.get("creators", []):
        if isinstance(creator, dict) and creator.get("name"):
            creators.append(Agent(name=creator["name"]))
    fair_profile = FairProfile(
        title=profile.get("title") or workflow.name,
        description=profile.get("description") or workflow.description or "",
        creators=creators,
        license=profile.get("license") or "",
        keywords=profile.get("keywords") or [],
        access_policy=AccessPolicy(metadata=profile.get("access_policy") or "local", data=profile.get("access_policy") or "local"),
        strict=bool(profile.get("strict", False)),
        output_dir=str(safe_child(run_dir, ".dagon", "fair", workflow.name)),
        # DAGonStar's capture_environment is the list of variables to record;
        # DAGonWeb stores a switch plus the allowlist, so pass the list only when on.
        capture_environment=list(profile.get("environment_allowlist") or []) if profile.get("capture_environment") else [],
    )
    runtime.enable_fair(fair_profile)


def prepare_native_task_environments(workflow: Workflow, root: Path) -> dict[str, dict[str, Any]]:
    """Install each Native task's declared requirements in its own scratch venv.

    Returns per-task ``DagonTask`` keyword overrides (the venv interpreter).
    """
    overrides: dict[str, dict[str, Any]] = {}
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
        overrides[task.name] = {"python": str(python)}
    return overrides


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


@contextmanager
def native_modules_importable(workflow: Workflow, root: Path) -> Iterator[None]:
    """Make embedded Native sources importable while DAGonStar resolves them."""
    module_root, modules = materialize_native_sources(workflow, root)
    with _IMPORT_LOCK:
        if module_root:
            sys.path.insert(0, str(module_root))
            importlib.invalidate_caches()
            for module in modules:
                sys.modules.pop(module, None)
        try:
            yield
        finally:
            if module_root and str(module_root) in sys.path:
                sys.path.remove(str(module_root))


def materialize_meta_workflows(workflow: Workflow, base: Path) -> dict[str, dict[str, Any]]:
    """Write inline Meta workflow definitions below ``base`` (the run directory).

    Returns ``DagonTask`` keyword overrides with the absolute definition path.
    """
    overrides: dict[str, dict[str, Any]] = {}
    sources = meta_workflow_sources((task.name, task.normalized_task_type, task.config) for task in workflow.tasks)
    if not sources:
        return overrides
    base = base.resolve()
    root = safe_child(base, META_WORKFLOW_ROOT)
    for name, relative, source in sources:
        target = safe_child(root, *relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        overrides[name] = {"workflow": str(target)}
    return overrides


def build_runtime_workflow(
    workflow: Workflow,
    config: dict[str, dict[str, Any]],
    *,
    checkpoint_file: str | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    interactive_data_dir: Path | None = None,
) -> Any:
    """Build a ``dagon.Workflow`` with one ``DagonTask`` per stored task.

    Data dependencies are discovered by DAGonStar from ``workflow://``
    references; explicit links become ``add_dependency_to`` edges, which
    DAGonStar serializes as ``explicit_prevs``.
    """
    from dagon import Workflow as DagonStarWorkflow
    from dagon.task import DagonTask, TaskType as DagonTaskType

    options = workflow.runtime_options
    runtime = DagonStarWorkflow(
        workflow.name,
        config=config,
        max_threads=max(1, min(int(options.get("max_threads") or 10), 256)),
        checkpoint_file=checkpoint_file,
        portable_emulation=bool(options.get("portable_emulation")),
        pre_stage=bool(options.get("pre_stage")),
    )
    overrides = dict(overrides or {})
    scratch_base = Path(str(config.get("batch", {}).get("scratch_dir_base") or scratch_root()))
    for name, values in materialize_meta_workflows(workflow, scratch_base).items():
        overrides[name] = {**overrides.get(name, {}), **values}
    tasks: dict[str, Any] = {}
    for task in workflow.tasks:
        task_type = task.normalized_task_type
        args, kwargs = dagon_task_arguments(task_type, task.name, task.config)
        if task_type == "web" and not task.config.get("python"):
            # DAGonStar's web runner defaults to a ``python`` executable that
            # may not exist in a virtualenv-only deployment.
            kwargs["python"] = sys.executable
        kwargs.update(overrides.get(task.name, {}))
        if task_type in INTERACTIVE_TASK_TYPES and interactive_data_dir is not None:
            # Bind DAGONWEB_DATA / DAGONWEB_PYTHON to this run.
            args[1] = substitute_runtime(args[1], str(interactive_data_dir), sys.executable)
        runtime_task = DagonTask(DagonTaskType[runtime_type(task_type).upper()], *args, **kwargs)
        runtime.add_task(runtime_task)
        tasks[task.name] = runtime_task
    for link in workflow.links:
        if link.kind == LINK_EXPLICIT and link.source_uid in tasks and link.target_uid in tasks:
            tasks[link.target_uid].add_dependency_to(tasks[link.source_uid])
    runtime.make_dependencies()
    return runtime


def workflow_config(workflow: Workflow, scratch_dir: Path) -> dict[str, dict[str, Any]]:
    base = runtime_dagon_config(Path(current_app.config["DAGON_INI_PATH"]), scratch_dir)
    merged = merge_workflow_dagon_config(base, workflow.dagon_ini)
    # Per-workflow settings never move task directories out of scratch.
    merged.setdefault("batch", {}).update({"scratch_dir_base": str(scratch_dir), "remove_dir": False})
    return merged


def export_cwl(workflow: Workflow) -> str:
    """Return DAGonStar's self-contained CWL v1.2 rendering of a workflow."""
    root = scratch_root()
    with tempfile.TemporaryDirectory(dir=root, prefix="cwl-export-") as temporary:
        export_dir = Path(temporary)
        with native_modules_importable(workflow, root):
            runtime = build_runtime_workflow(workflow, workflow_config(workflow, export_dir))
        target = export_dir / f"{workflow.name}.cwl"
        runtime.saveAsCWL(str(target))
        return target.read_text(encoding="utf-8")


def run_checkpoint_path(run_dir: Path) -> Path:
    return safe_child(run_dir, *CHECKPOINT_FILE)


def run_data_path(run_dir: Path, *parts: str) -> Path:
    """Folder (below the run directory) for interactive task data."""
    return safe_child(run_dir, *INTERACTIVE_DATA_DIR, *parts)


def _task_status(task: Any) -> str:
    return {"RUNNING": RunStatus.RUNNING.value, "FINISHED": RunStatus.SUCCESS.value, "FAILED": RunStatus.FAILED.value}.get(
        getattr(getattr(task, "status", None), "name", ""), RunStatus.PENDING.value
    )


class TaskFailureCapture(logging.Handler):
    """Keep the exception DAGonStar logs when one of *these* tasks fails.

    DAGonStar task threads log failures with ``logger.exception`` and do not
    re-raise; the handler is filtered by thread so concurrent runs never see
    each other's errors.
    """

    def __init__(self, tasks: list[Any]) -> None:
        super().__init__(level=logging.ERROR)
        self.tasks = {id(task): task for task in tasks}
        self.failures: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        task = self.tasks.get(id(threading.current_thread()))
        if task is None or not record.exc_info or record.exc_info[1] is None:
            return
        self.failures[task.name] = str(record.exc_info[1]).strip()[-4000:]


def _failure_detail(task: Any, failures: dict[str, str]) -> str:
    if failures.get(task.name):
        return failures[task.name]
    if any(_task_status(previous) == RunStatus.FAILED.value for previous in getattr(task, "prevs", [])):
        return "skipped because an upstream task failed"
    result = getattr(task, "result", None)
    if isinstance(result, dict):
        message = str(result.get("message") or "").strip()
        if message:
            return message[-4000:]
    return ""


def execute_workflow(workflow: Workflow, user_id: int, run: WorkflowRun | None = None, *, resume: bool = False) -> WorkflowRun:
    """Execute ``workflow`` with DAGonStar and keep ``run`` updated.

    With ``resume=True`` the run directory and DAGonStar checkpoint of an
    earlier attempt are reused: tasks that completed successfully are
    skipped by DAGonStar and the others run again.
    """
    root = scratch_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if run is None:
        run_dir = safe_child(root, f"workflow-{workflow.id}", f"run-{stamp}")
        run = WorkflowRun(workflow_id=workflow.id, user_id=user_id, status=RunStatus.RUNNING.value, scratch_path=str(run_dir), log="")
        db.session.add(run)
    else:
        existing = Path(run.scratch_path).resolve() if run.scratch_path else None
        run_dir = existing if resume and existing and is_inside(root, existing) else safe_child(root, f"workflow-{workflow.id}", f"run-{run.id}")
        run.status = RunStatus.RUNNING.value
        run.scratch_path = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = run_checkpoint_path(run_dir)
    data_dir = run_data_path(run_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    resume_file = str(checkpoint) if resume and checkpoint.is_file() else None
    run.log = (run.log or "") + timestamped_log("Workflow resumed from checkpoint." if resume_file else "Workflow started.")
    for task_run in list(run.task_runs):
        db.session.delete(task_run)
    db.session.commit()
    run_id = run.id
    app = current_app._get_current_object()
    lock = threading.Lock()
    capture: TaskFailureCapture | None = None
    try:
        overrides = prepare_native_task_environments(workflow, root)
        with native_modules_importable(workflow, root):
            runtime = build_runtime_workflow(workflow, workflow_config(workflow, run_dir), checkpoint_file=str(checkpoint), overrides=overrides, interactive_data_dir=data_dir)
        enable_fair_if_configured(runtime, workflow, run_dir)
        task_runs = {task.name: TaskRun(workflow_run_id=run_id, task_uid=task.name, status=RunStatus.PENDING.value, scratch_path=str(run_dir), log="") for task in runtime.tasks}
        db.session.add_all(task_runs.values())
        db.session.commit()
        task_run_ids = {name: task_run.id for name, task_run in task_runs.items()}
        capture = TaskFailureCapture(runtime.tasks)
        logging.getLogger().addHandler(capture)

        def record(task: Any, event: str | None = None) -> None:
            """Persist a DAGonStar task event; called from DAGonStar threads."""
            with lock, app.app_context():
                task_run = db.session.get(TaskRun, task_run_ids.get(task.name))
                current_run = db.session.get(WorkflowRun, run_id)
                if not task_run or not current_run:
                    return
                status = _task_status(task)
                messages = []
                if event:
                    messages.append(f"Task {task.name}: {event}.")
                if status != task_run.status:
                    verb = {RunStatus.RUNNING.value: "started", RunStatus.SUCCESS.value: "completed", RunStatus.FAILED.value: "failed"}.get(status)
                    if verb:
                        messages.append(f"Task {task.name} {verb}.")
                    detail = _failure_detail(task, capture.failures) if status == RunStatus.FAILED.value else ""
                    if detail:
                        messages.append(f"Task {task.name} error: {detail}")
                task_run.status = status
                working_dir = getattr(task, "working_dir", None)
                if working_dir and is_inside(root, Path(working_dir)):
                    task_run.scratch_path = str(Path(working_dir).resolve())
                for message in messages:
                    line = timestamped_log(message)
                    task_run.log = (task_run.log or "") + line
                    current_run.log = (current_run.log or "") + line
                db.session.commit()

        runtime.on_task_start += lambda task: record(task)
        runtime.on_task_end += lambda task: record(task)
        for event_name, label in _TASK_EVENTS.items():
            runtime.add_listener(event_name, lambda task, label=label: record(task, label))
        runtime.launch(resume_checkpoint_file=resume_file)
        runtime.wait()
        for runtime_task in runtime.tasks:
            record(runtime_task)
        db.session.expire_all()
        run = db.session.get(WorkflowRun, run_id)
        run.status = RunStatus.SUCCESS.value if all(task.status.name == "FINISHED" for task in runtime.tasks) else RunStatus.FAILED.value
        run.log = (run.log or "") + timestamped_log(f"DAGonStar workflow completed with status: {run.status}.")
    except Exception as exc:
        db.session.rollback()
        run = db.session.get(WorkflowRun, run_id)
        run.status = RunStatus.FAILED.value
        run.log = (run.log or "") + timestamped_log(f"DAGonStar workflow failed: {exc}")
    finally:
        if capture is not None:
            logging.getLogger().removeHandler(capture)
    db.session.commit()
    return run


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
    root = scratch_root()
    items = []
    for child in sorted(base.iterdir()):
        resolved = child.resolve()
        if root not in resolved.parents and resolved != root:
            continue
        stat = child.stat()
        items.append({"name": child.name, "is_dir": child.is_dir(), "size": stat.st_size, "modified_at": stat.st_mtime})
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
