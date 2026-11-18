"""Guest playground: try DAGonWeb without an account, in dry-run mode.

Visitors open a template (or an empty canvas) in the normal editor. The
workflow lives in the browser tab (``sessionStorage``) and is never stored
on the server. "Run" performs a *dry run*: DAGonWeb checks the graph, the
shell and Python syntax, builds the DAGonStar tasks in portable emulation,
and returns the execution plan, which the editor plays back on the canvas.
No task is executed, no file is written for the visitor, and no Native code
is imported.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, jsonify, render_template, request, url_for
from flask_login import current_user

from . import settings
from .agents.tools import Draft, validate_workflow
from .models import User
from .task_specs import COMMAND_TYPES, INTERACTIVE_TASK_TYPES, TASK_SPECS, dagon_task_arguments, runtime_type

bp = Blueprint("playground", __name__)

MAX_DOCUMENT_BYTES = 512 * 1024
MAX_TASKS = 60
PREVIEW_CHARS = 2400


def enabled() -> bool:
    return bool(settings.get("guest_playground_enabled"))


def _require_enabled() -> None:
    if not enabled():
        abort(404)


@bp.get("/")
def index():
    _require_enabled()
    from .workflows.routes import workflow_template_cards

    return render_template("playground/index.html", templates=workflow_template_cards())


@bp.get("/<template>")
def editor(template: str):
    _require_enabled()
    if template != "empty" and template not in TASK_SPECS:
        abort(404)
    from .workflows.routes import TEMPLATE_DESCRIPTIONS, palette

    name = "my_workflow" if template == "empty" else f"{template}_example"
    return render_template(
        "workflows/editor.html",
        guest=True,
        workflow=None,
        workflow_name=name,
        template=template,
        template_description=TEMPLATE_DESCRIPTIONS.get(template, ""),
        editable=True,
        palette=palette(),
        graph_url=url_for("playground.template_json", template=template),
        dry_run_url=url_for("playground.dry_run"),
        assistant_enabled=False,
        registration_open=bool(settings.get("registration_enabled")),
    )


@bp.get("/<template>.json")
def template_json(template: str):
    _require_enabled()
    if template == "empty":
        return jsonify({"name": "my_workflow", "id": None, "host": "localhost", "tasks": {}, "dagonweb": {"runtime": {"portable_emulation": True}}})
    if template not in TASK_SPECS:
        abort(404)
    from .workflows.routes import template_document

    owner = User.query.order_by(User.id).first()
    if owner is None:
        abort(503)
    document = template_document(template, owner.id)
    document["dagonweb"] = {**(document.get("dagonweb") or {}), "runtime": {**((document.get("dagonweb") or {}).get("runtime") or {}), "portable_emulation": True}}
    return jsonify(document)


@bp.post("/dry-run")
def dry_run():
    """Validate a workflow and describe how DAGonStar would execute it."""
    if not enabled() and not current_user.is_authenticated:
        abort(404)
    if (request.content_length or 0) > MAX_DOCUMENT_BYTES:
        return jsonify({"ok": False, "error": "The workflow is too large for a dry run."}), 413
    document = request.get_json(silent=True)
    if not isinstance(document, dict) or not isinstance(document.get("tasks"), dict):
        return jsonify({"ok": False, "error": "Send a DAGonStar document with a tasks object."}), 400
    if len(document["tasks"]) > MAX_TASKS:
        return jsonify({"ok": False, "error": f"Dry runs accept at most {MAX_TASKS} tasks."}), 400
    return jsonify(dry_run_plan(document))


def dry_run_plan(document: dict[str, Any]) -> dict[str, Any]:
    """The validation report plus stages and per-task previews."""
    from .workflows.routes import validate_graph_payload

    draft = Draft.from_document(document)
    report = validate_workflow(draft, import_native=False)
    plan: dict[str, Any] = {
        "ok": bool(report.get("ok")),
        "problems": list(report.get("problems") or ([report["error"]] if report.get("error") else [])),
        "warnings": list(report.get("warnings") or []),
        "dagonstar": report.get("dagonstar", ""),
        "stages": [],
        "tasks": {},
    }
    try:
        _, links = validate_graph_payload(draft.document())
    except (ValueError, TypeError):
        return plan
    names = [task["name"] for task in draft.tasks]
    parents: dict[str, set[str]] = {name: set() for name in names}
    for link in links:
        if link["target_name"] in parents and link["source_name"] in parents:
            parents[link["target_name"]].add(link["source_name"])
    level: dict[str, int] = {}
    remaining = set(names)
    while remaining:
        ready = sorted(name for name in remaining if parents[name] <= set(level))
        if not ready:  # cycles are reported by validate_graph_payload
            break
        for name in ready:
            level[name] = 1 + max((level[parent] for parent in parents[name]), default=-1)
        remaining -= set(ready)
    stages: dict[int, list[str]] = {}
    for name, index in level.items():
        stages.setdefault(index, []).append(name)
    plan["stages"] = [sorted(stages[index]) for index in sorted(stages)]
    for task in draft.tasks:
        plan["tasks"][task["name"]] = _task_preview(task, links)
    return plan


def _task_preview(task: dict[str, Any], links: list[dict[str, Any]]) -> dict[str, Any]:
    task_type = task["task_type"]
    spec = TASK_SPECS.get(task_type)
    preview: dict[str, Any] = {
        "type": task_type,
        "label": spec.label if spec else task_type,
        "runtime_type": runtime_type(task_type) if spec else task_type,
        "after": sorted({link["source_name"] for link in links if link["target_name"] == task["name"]}),
        "stages_in": sorted({f"{link['source_name']}/{link['source_output']}" for link in links if link["target_name"] == task["name"] and link.get("kind") == "data" and link.get("source_output")}),
        "would": _would(task_type),
        "preview": "",
    }
    try:
        args, kwargs = dagon_task_arguments(task_type, task["name"], task["config"])
    except Exception as exc:  # reported by validate_workflow too
        preview["preview"] = f"Cannot build the task: {exc}"
        return preview
    if spec and runtime_type(task_type) in COMMAND_TYPES and len(args) > 1 and task_type not in INTERACTIVE_TASK_TYPES:
        text = str(args[1])
    else:
        shown = {key: value for key, value in kwargs.items() if "key" not in key.lower() and "secret" not in key.lower() and "password" not in key.lower()}
        text = f"DagonTask(TaskType.{str(runtime_type(task_type)).upper()}, {', '.join(repr(arg)[:200] for arg in args[:1])}{', ' if shown else ''}{', '.join(f'{key}={value!r}'[:200] for key, value in shown.items())})"
    preview["preview"] = text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "\n…"
    return preview


def _would(task_type: str) -> str:
    return {
        "upload": "Pause the run until someone uploads files on the run page.",
        "form": "Pause the run until someone fills the form on the run page.",
        "table": "Render its input as a table on the run page.",
        "chart": "Draw a chart of its input on the run page.",
        "download": "Offer its inputs as downloads on the run page.",
        "native": "Call the Python function in a per-run virtual environment.",
        "llm": "Send the prompt to the configured LLM (locally emulated in portable mode).",
        "agentic": "Run the bounded agent contract (locally emulated in portable mode).",
    }.get(task_type, "Run the command in its own working directory (container and scheduler back-ends are emulated locally in portable mode).")
