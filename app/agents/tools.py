"""Tools the workflow assistant can call.

Every tool works on an in-memory *draft* of a DAGonWeb workflow; nothing is
saved until the user applies the draft in the editor. The tools never run
user commands: ``validate_workflow`` performs a dry run (graph checks, a
DAGonStar ``DagonTask`` build, ``bash -n`` and Python syntax checks) and
``diagnose_last_run`` only reads the logs of a run the user started.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from ..models import LINK_EXPLICIT, TaskType, WorkflowRun
from ..task_specs import COMMAND_TYPES, INTERACTIVE_TASK_TYPES, TASK_SPECS, dagon_task_arguments, runtime_type

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_TASKS = 60


@dataclass
class Draft:
    """Mutable workflow draft shared by the tools of one assistant turn."""

    name: str
    tasks: list[dict[str, Any]]
    workflow_id: int | None = None
    user_id: int | None = None
    is_admin: bool = False
    changed: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_document(cls, document: dict[str, Any], **extra: Any) -> "Draft":
        tasks = []
        for name, entry in (document.get("tasks") or {}).items():
            extension = entry.get("dagonweb") or {}
            task_type = extension.get("task_type") or entry.get("type") or "batch"
            tasks.append({
                "name": entry.get("name", name),
                "task_type": task_type,
                "x": extension.get("x"),
                "y": extension.get("y"),
                "config": copy.deepcopy(extension.get("config") or {}),
                "explicit_prevs": list(entry.get("explicit_prevs") or []),
            })
        return cls(name=str(document.get("name") or "workflow"), tasks=tasks, **extra)

    def document(self) -> dict[str, Any]:
        """Editor-format document accepted by ``validate_graph_payload``."""
        return {
            "name": self.name,
            "tasks": {
                task["name"]: {
                    "name": task["name"],
                    "type": task["task_type"],
                    "command": "",
                    "explicit_prevs": list(task.get("explicit_prevs") or []),
                    "dagonweb": {"x": task.get("x") if task.get("x") is not None else 100, "y": task.get("y") if task.get("y") is not None else 100, "config": task.get("config") or {}},
                }
                for task in self.tasks
            },
        }

    def find(self, name: str) -> dict[str, Any] | None:
        return next((task for task in self.tasks if task["name"] == name), None)


# ---------------------------------------------------------------- helpers


def _layout(draft: Draft) -> None:
    """Place tasks without coordinates in dependency layers."""
    from ..workflows.routes import apply_import_layout, validate_graph_payload

    if all(task.get("x") is not None and task.get("y") is not None for task in draft.tasks):
        return
    try:
        tasks, links = validate_graph_payload(draft.document())
    except ValueError:
        return
    apply_import_layout(tasks, links)
    positions = {task["name"]: (task["x"], task["y"]) for task in tasks}
    for task in draft.tasks:
        if task.get("x") is None or task.get("y") is None:
            task["x"], task["y"] = positions.get(task["name"], (100, 100))


def _check(draft: Draft) -> dict[str, Any]:
    from ..workflows.routes import validate_graph_payload

    try:
        _, links = validate_graph_payload(draft.document())
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "tasks": len(draft.tasks),
        "dependencies": [f"{link['source_name']} -> {link['target_name']} ({link['kind']})" for link in links],
    }


def _normalize_task(raw: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Each task must be an object.")
    name = str(raw.get("name") or (previous or {}).get("name") or "")
    if not NAME_RE.match(name):
        raise ValueError(f"Invalid task name {name!r}: use letters, digits, _ and -.")
    task_type = str(raw.get("task_type") or raw.get("type") or (previous or {}).get("task_type") or "batch").lower()
    if task_type not in TASK_SPECS:
        raise ValueError(f"Unknown task type {task_type!r}. Call list_task_types.")
    config = raw.get("config")
    if config is None:
        config = copy.deepcopy((previous or {}).get("config") or TASK_SPECS[task_type].default_config)
    if not isinstance(config, dict):
        raise ValueError(f"Task {name}: config must be an object.")
    explicit = raw.get("explicit_prevs", (previous or {}).get("explicit_prevs") or [])
    if not isinstance(explicit, list):
        raise ValueError(f"Task {name}: explicit_prevs must be a list of task names.")
    return {"name": name, "task_type": task_type, "config": config, "explicit_prevs": [str(item) for item in explicit], "x": raw.get("x", (previous or {}).get("x")), "y": raw.get("y", (previous or {}).get("y"))}


# ---------------------------------------------------------------- tools


def list_task_types(draft: Draft, **_: Any) -> dict[str, Any]:
    types = []
    for key, spec in TASK_SPECS.items():
        types.append({
            "type": key,
            "label": spec.label,
            "description": spec.description,
            "runs_as": spec.runtime_type,
            "config_fields": [f"{item['key']} ({item['kind']})" for item in spec.fields],
            "default_config": spec.default_config,
        })
    return {"task_types": types}


def get_workflow(draft: Draft, **_: Any) -> dict[str, Any]:
    return {"name": draft.name, "tasks": [{key: task[key] for key in ("name", "task_type", "config", "explicit_prevs")} for task in draft.tasks], "check": _check(draft)}


def set_workflow(draft: Draft, tasks: list[dict[str, Any]] | None = None, **_: Any) -> dict[str, Any]:
    if not isinstance(tasks, list) or not tasks:
        return {"ok": False, "error": "Provide a non-empty list of tasks."}
    if len(tasks) > MAX_TASKS:
        return {"ok": False, "error": f"At most {MAX_TASKS} tasks."}
    try:
        previous = {task["name"]: task for task in draft.tasks}
        normalized = [_normalize_task(raw, previous.get(str(raw.get("name")))) for raw in tasks]
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    backup = draft.tasks
    draft.tasks = normalized
    result = _check(draft)
    if not result["ok"]:
        draft.tasks = backup
        return result
    draft.changed = True
    _layout(draft)
    return result


def upsert_task(draft: Draft, name: str = "", task_type: str | None = None, config: dict[str, Any] | None = None, config_patch: dict[str, Any] | None = None, explicit_prevs: list[str] | None = None, rename_to: str | None = None, **_: Any) -> dict[str, Any]:
    previous = draft.find(name)
    raw: dict[str, Any] = {"name": rename_to or name}
    if task_type:
        raw["task_type"] = task_type
    if config is not None:
        raw["config"] = config
    elif config_patch:
        if not isinstance(config_patch, dict):
            return {"ok": False, "error": "config_patch must be an object."}
        base = copy.deepcopy((previous or {}).get("config") or TASK_SPECS.get(task_type or "batch", TASK_SPECS["batch"]).default_config)
        base.update(config_patch)
        raw["config"] = base
    if explicit_prevs is not None:
        raw["explicit_prevs"] = explicit_prevs
    if previous and (task_type and task_type != previous["task_type"]) and config is None and not config_patch:
        raw["config"] = copy.deepcopy(TASK_SPECS[task_type].default_config) if task_type in TASK_SPECS else None
    try:
        task = _normalize_task(raw, previous)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    backup = copy.deepcopy(draft.tasks)
    if previous:
        draft.tasks[draft.tasks.index(previous)] = task
        if rename_to and rename_to != name:
            for other in draft.tasks:
                other["explicit_prevs"] = [rename_to if item == name else item for item in other.get("explicit_prevs") or []]
                other["config"] = json.loads(json.dumps(other["config"]).replace(f"workflow:///{name}/", f"workflow:///{rename_to}/"))
    else:
        if len(draft.tasks) >= MAX_TASKS:
            return {"ok": False, "error": f"At most {MAX_TASKS} tasks."}
        draft.tasks.append(task)
    result = _check(draft)
    if not result["ok"]:
        draft.tasks = backup
        return result
    draft.changed = True
    _layout(draft)
    return result


def remove_task(draft: Draft, name: str = "", **_: Any) -> dict[str, Any]:
    task = draft.find(name)
    if not task:
        return {"ok": False, "error": f"No task named {name}."}
    backup = copy.deepcopy(draft.tasks)
    draft.tasks.remove(task)
    for other in draft.tasks:
        other["explicit_prevs"] = [item for item in other.get("explicit_prevs") or [] if item != name]
    result = _check(draft)
    if not result["ok"]:
        draft.tasks = backup
        return {**result, "error": result["error"] + " Remove or rewrite the tasks that reference it first."}
    draft.changed = True
    return result


def validate_workflow(draft: Draft, **_: Any) -> dict[str, Any]:
    """Dry run: graph checks, DAGonStar task construction and syntax checks."""
    from flask import current_app

    from ..executor.local import build_runtime_workflow, native_modules_importable, scratch_root

    report = _check(draft)
    if not report["ok"]:
        return report
    problems: list[str] = []
    warnings: list[str] = []
    for task in draft.tasks:
        task_type, config = task["task_type"], task["config"]
        try:
            args, _kwargs = dagon_task_arguments(task_type, task["name"], config)
        except Exception as exc:
            problems.append(f"{task['name']}: {exc}")
            continue
        if runtime_type(task_type) in COMMAND_TYPES and task_type != "checkpoint":
            script = str(args[1])
            if not script.strip() and task_type not in INTERACTIVE_TASK_TYPES:
                problems.append(f"{task['name']}: the command is empty.")
            try:
                checked = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, timeout=5)
                if checked.returncode:
                    problems.append(f"{task['name']}: shell syntax error: {checked.stderr.strip()[:300]}")
            except (OSError, subprocess.TimeoutExpired):
                warnings.append("bash is not available for syntax checks.")
        if task_type == "native" and str(config.get("source") or "").strip():
            try:
                compile(config["source"], f"{task['name']}.py", "exec")
            except SyntaxError as exc:
                problems.append(f"{task['name']}: Python syntax error line {exc.lineno}: {exc.msg}")
        if task_type in ("table", "chart") and "source" not in (config.get("inputs") or {}):
            problems.append(f"{task['name']}: set inputs.source to a workflow:///task/file reference.")
        if task_type in ("upload", "form"):
            warnings.append(f"{task['name']} waits for user input on the run page.")
    if not problems:
        workflow = SimpleNamespace(
            id=f"assistant-{draft.workflow_id or 0}",
            name=draft.name,
            runtime_options={"portable_emulation": True},
            tasks=[SimpleNamespace(name=task["name"], normalized_task_type=task["task_type"], config=task["config"]) for task in draft.tasks],
            links=[SimpleNamespace(kind=LINK_EXPLICIT, source_uid=source, target_uid=task["name"]) for task in draft.tasks for source in task.get("explicit_prevs") or []],
        )
        try:
            root = scratch_root()
            config = {"batch": {"scratch_dir_base": str(root), "remove_dir": False}, "ftp_pub": {}, "dagon_service": {"use": "False"}}
            with native_modules_importable(workflow, root):
                runtime = build_runtime_workflow(workflow, config)
            report["dagonstar"] = f"DAGonStar accepted {len(runtime.tasks)} tasks."
        except Exception as exc:
            current_app.logger.info("Assistant dry run failed: %s", exc)
            problems.append(f"DAGonStar rejected the workflow: {exc}")
    report.update({"ok": not problems, "problems": problems, "warnings": sorted(set(warnings))})
    return report


def diagnose_last_run(draft: Draft, **_: Any) -> dict[str, Any]:
    if not draft.workflow_id:
        return {"ok": False, "error": "The workflow has not been saved or run yet."}
    query = WorkflowRun.query.filter_by(workflow_id=draft.workflow_id)
    if not draft.is_admin:
        query = query.filter_by(user_id=draft.user_id)
    run = query.order_by(WorkflowRun.created_at.desc()).first()
    if not run:
        return {"ok": False, "error": "No runs yet. Ask the user to press Run, then diagnose."}
    tasks = [{"name": task_run.task_uid, "status": task_run.status, "log_tail": (task_run.log or "")[-1500:]} for task_run in run.task_runs]
    return {"ok": True, "run_id": run.id, "status": run.status, "tasks": tasks, "run_log_tail": (run.log or "")[-3000:]}


TOOLS: dict[str, tuple[Callable[..., dict[str, Any]], str, dict[str, Any]]] = {
    "list_task_types": (list_task_types, "List every task type with its configuration fields and default configuration.", {"type": "object", "properties": {}}),
    "get_workflow": (get_workflow, "Return the current draft workflow and its dependency check.", {"type": "object", "properties": {}}),
    "set_workflow": (
        set_workflow,
        "Replace the whole draft. Use it to design a new workflow. Dependencies come from workflow:///<task>/<path> references in the config and from explicit_prevs.",
        {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "task_type": {"type": "string", "enum": [member.value for member in TaskType]},
                            "config": {"type": "object"},
                            "explicit_prevs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "task_type", "config"],
                    },
                }
            },
            "required": ["tasks"],
        },
    ),
    "upsert_task": (
        upsert_task,
        "Add or change one task. Use config to replace the configuration or config_patch to merge keys; rename_to renames the task and rewrites references.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_type": {"type": "string", "enum": [member.value for member in TaskType]},
                "config": {"type": "object"},
                "config_patch": {"type": "object"},
                "explicit_prevs": {"type": "array", "items": {"type": "string"}},
                "rename_to": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    "remove_task": (remove_task, "Remove one task from the draft.", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    "validate_workflow": (validate_workflow, "Dry-run the draft: dependency graph, DAGonStar task construction, shell and Python syntax. Call it before you finish.", {"type": "object", "properties": {}}),
    "diagnose_last_run": (diagnose_last_run, "Read the status and logs of the user's latest run of this workflow to explain or fix failures.", {"type": "object", "properties": {}}),
}


def tool_schemas() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": name, "description": description, "parameters": parameters}} for name, (_, description, parameters) in TOOLS.items()]


def call_tool(draft: Draft, name: str, arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    if name not in TOOLS:
        return {"ok": False, "error": f"Unknown tool {name}."}
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Arguments are not valid JSON: {exc}"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "Arguments must be an object."}
    try:
        return TOOLS[name][0](draft, **parsed)
    except TypeError as exc:
        return {"ok": False, "error": f"Bad arguments: {exc}"}

