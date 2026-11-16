from __future__ import annotations

import json
import re
import shutil
from configparser import Error as ConfigParserError
from io import BytesIO
from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile
from pathlib import Path
import networkx as nx
from sqlalchemy import false as sa_false
from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from ..extensions import db
from ..dagon_ini import dump_dagon_ini, parse_dagon_ini
from ..models import ACTIVE_RUN_STATUSES, LINK_DATA, LINK_EXPLICIT, TaskRun, TaskType, Workflow, WorkflowLink, WorkflowTask, WorkflowRun
from ..executor.local import execute_workflow, export_cwl, list_directory, list_files, preview_task_file, safe_child, scratch_root
from ..task_specs import CATEGORIES, TASK_SPECS, TASK_TYPE_LABELS, WORKFLOW_REFERENCE, config_from_document, dagon_task_arguments, editor_references, palette
from .forms import DAGONSTAR_NAME_RE, RunForm, WorkflowForm, WorkflowSetupForm

bp = Blueprint("workflows", __name__)
FAIR_EXPORT_FILES = {
    "run.json": "Run metadata",
    "ro-crate-metadata.json": "RO-Crate JSON-LD",
    "prov.json": "PROV graph",
    "datacite.json": "DataCite JSON",
    "codemeta.json": "CodeMeta JSON-LD",
    "checksums.sha256": "SHA-256 checksums",
    "fairness-report.json": "FAIR validation report",
    "report.md": "Markdown report",
    "report.html": "HTML report",
}


def validate_dagonstar_name(name: str, label: str = "Name") -> str:
    value = name.strip()
    if not re.fullmatch(DAGONSTAR_NAME_RE, value) or len(value) > 80:
        raise ValueError(f"{label} may contain only letters, numbers, underscores, and hyphens.")
    return value


def is_safe_workflow_path(path: str) -> bool:
    if not path or len(path) > 255 or path.startswith("/"):
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} and re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)


def rewrite_workflow_reference_names(value: object, old_name: str, new_name: str) -> object:
    """Update named same-workflow references after a workflow rename."""
    if isinstance(value, dict):
        return {key: rewrite_workflow_reference_names(item, old_name, new_name) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_workflow_reference_names(item, old_name, new_name) for item in value]
    if isinstance(value, str):
        return re.sub(rf"workflow://{re.escape(old_name)}(?=/)", f"workflow://{new_name}", value)
    return value


TEMPLATE_DESCRIPTIONS = {
    "batch": "Four Batch tasks in a diamond: one producer, two transformers, one merge.",
    "checkpoint": "A producer followed by a Checkpoint so failed runs can resume.",
    "faas": "Batch → FaaS (mock provider) → Batch, with no credentials needed.",
    "iot": "A bounded IoT observation from the credential-free mock provider.",
    "agentic": "A producer and a bounded Agentic quality-control task (mock adapter).",
}
# Templates for external back-ends enable DAGonStar portable emulation, so the
# example runs locally until the real back-end is configured.
EMULATED_TEMPLATES = {"slurm", "cloud", "docker", "kubernetes", "apptainer", "nomad", "llm"}
AGENTIC_TEMPLATE_INI = {
    "agent_provider.fixture": {"implementation": "mock"},
    "agent.quality-local": {
        "provider": "fixture",
        "strategy": "bounded_iterative",
        "tools": "inspect_records, repair_records",
        "guardrails": "preserve_record_ids",
        "max_steps": "8",
        "max_tool_calls": "8",
        "timeout_seconds": "60",
        "max_tokens": "4096",
        "max_tool_output_bytes": "65536",
        "max_trace_bytes": "262144",
    },
}
OBSERVATIONS_COMMAND = (
    "mkdir -p outputs\n"
    "printf '%s' '{\"observations\": [{\"id\": 1, \"station\": \"SYN001\", \"temperature_c\": 12.0}, "
    "{\"id\": 2, \"station\": \"SYN001\", \"temperature_c\": 14.0}, {\"id\": 2, \"station\": \"SYN001\", \"temperature_c\": 14.0}, "
    "{\"id\": 3, \"station\": \"SYN002\", \"temperature_c\": 11.5}, {\"id\": 4, \"station\": \"SYN002\", \"temperature_c\": 13.5}]}' "
    "> outputs/observations.json"
)


def workflow_template_options() -> list[tuple[str, str]]:
    return [("batch", "Batch diamond"), *[(task_type, label) for task_type, label in TASK_TYPE_LABELS.items() if task_type != "batch"]]


def workflow_template_cards() -> list[dict]:
    cards = []
    for key, label in workflow_template_options():
        spec = TASK_SPECS[key]
        description = TEMPLATE_DESCRIPTIONS.get(key) or spec.description
        if key in EMULATED_TEMPLATES:
            description += " Starts in portable emulation, so it runs locally until the back-end is configured."
        cards.append({"key": key, "label": label, "icon": spec.icon, "category": spec.category, "description": description})
    return cards


def add_workflow_task(workflow: Workflow, name: str, task_type: str, x: int, y: int, config: dict) -> WorkflowTask:
    task = WorkflowTask(workflow_id=workflow.id, uid=name, label=name, task_type=task_type, x=x, y=y)
    task.config = config
    workflow.tasks.append(task)
    return task


def apply_workflow_template(workflow: Workflow, template: str) -> None:
    if template not in TASK_SPECS:
        raise ValueError("Unknown workflow template.")
    if template == "batch":
        add_workflow_task(workflow, "extract", "batch", 120, 220, {"command": "echo hello > data.txt", "inputs": {}})
        add_workflow_task(workflow, "transform_a", "batch", 400, 120, {"command": "tr a-z A-Z < $data > a.txt", "inputs": {"data": "workflow:///extract/data.txt"}})
        add_workflow_task(workflow, "transform_b", "batch", 400, 320, {"command": "rev $data > b.txt", "inputs": {"data": "workflow:///extract/data.txt"}})
        add_workflow_task(workflow, "merge", "batch", 680, 220, {"command": "cat workflow:///transform_a/a.txt workflow:///transform_b/b.txt > result.txt", "inputs": {}})
    elif template == "checkpoint":
        add_workflow_task(workflow, "simulate", "batch", 120, 200, {"command": "mkdir -p output\necho 42 > output/state.txt", "inputs": {}})
        add_workflow_task(workflow, "checkpoint", "checkpoint", 400, 200, {"command": "", "inputs": {"state": "workflow:///simulate/output/state.txt"}})
        add_workflow_task(workflow, "report", "batch", 680, 200, {"command": "cat $state > report.txt", "inputs": {"state": "workflow:///simulate/output/state.txt"}})
    elif template == "faas":
        add_workflow_task(workflow, "prepare", "batch", 120, 200, {"command": "mkdir -p outputs\nprintf '{\"value\": 4}' > outputs/input.json", "inputs": {}})
        config = dict(TASK_SPECS["faas"].default_config)
        config.update({"function": "double-value", "inputs": {"request": {"document": "workflow:///prepare/outputs/input.json"}}})
        add_workflow_task(workflow, "invoke", "faas", 400, 200, config)
        add_workflow_task(workflow, "consume", "batch", 680, 200, {"command": "cat workflow:///invoke/outputs/result.json > summary.json", "inputs": {}})
    elif template == "agentic":
        add_workflow_task(workflow, "prepare", "batch", 120, 200, {"command": OBSERVATIONS_COMMAND, "inputs": {}})
        config = json.loads(json.dumps(TASK_SPECS["agentic"].default_config))
        config["inputs"] = {"source": "workflow:///prepare/outputs/observations.json"}
        add_workflow_task(workflow, "quality_control", "agentic", 420, 200, config)
        workflow.dagon_ini = {**workflow.dagon_ini, **AGENTIC_TEMPLATE_INI}
    else:
        spec = TASK_SPECS[template]
        add_workflow_task(workflow, spec.default_name, template, 160, 160, json.loads(json.dumps(spec.default_config)))
        if template in EMULATED_TEMPLATES:
            workflow.runtime_options = {**workflow.runtime_options, "portable_emulation": True}
    db.session.flush()
    rebuild_reference_links(workflow)


def template_document(template: str, owner_id: int) -> dict:
    """Return the DAGonStar document a template produces, without persisting it."""
    workflow = Workflow(owner_id=owner_id, name=f"{template}_example", description=TEMPLATE_DESCRIPTIONS.get(template, ""))
    db.session.add(workflow)
    try:
        db.session.flush()
        apply_workflow_template(workflow, template)
        db.session.flush()
        document = workflow.as_json()
    finally:
        db.session.rollback()
    document["id"] = None
    return document


def rebuild_reference_links(workflow: Workflow) -> None:
    """Derive data links from ``workflow://`` references of stored tasks."""
    task_names = {task.name for task in workflow.tasks}
    links: set[tuple[str, str]] = {(link.source_uid, link.target_uid) for link in workflow.links}
    for task in workflow.tasks:
        task_data = {"name": task.name, "task_type": task.normalized_task_type, "config": task.config}
        for workflow_name, source_uid, source_output, target_input in task_workflow_references(task_data, workflow.name):
            key = (source_uid, task.name)
            if workflow_name in {"", workflow.name} and source_uid in task_names and source_uid != task.name and key not in links:
                links.add(key)
                workflow.links.append(
                    WorkflowLink(
                        workflow_id=workflow.id,
                        source_uid=source_uid,
                        source_output=source_output,
                        target_uid=task.name,
                        target_input=target_input,
                        workflow_uri=f"workflow:///{source_uid}/{source_output}",
                        kind=LINK_DATA,
                    )
                )

def can_access(workflow: Workflow) -> bool:
    return workflow.owner_id == current_user.id or current_user.has_role("admin") or workflow.is_public

def can_edit(workflow: Workflow) -> bool:
    return workflow.owner_id == current_user.id or current_user.has_role("admin")


def can_access_run(run: WorkflowRun) -> bool:
    return run.user_id == current_user.id or current_user.has_role("admin")


def can_manage_run(run: WorkflowRun) -> bool:
    """Only the creator (or an administrator) may change a run record."""
    return can_access_run(run)


def remove_run_scratch(run: WorkflowRun) -> None:
    """Remove a completed run's artifacts only when they are under scratch."""
    if not run.scratch_path:
        return
    root = scratch_root()
    run_root = Path(run.scratch_path).resolve()
    if root in run_root.parents and run_root.is_dir():
        shutil.rmtree(run_root)


def split_lines(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n,]+", value or "") if item.strip()]


def fair_profile_from_form(form: WorkflowSetupForm, workflow: Workflow) -> dict:
    creators = [{"name": name} for name in split_lines(form.fair_creators.data or "")]
    return {
        "enabled": bool(form.fair_enabled.data),
        "title": form.fair_title.data or workflow.name,
        "description": form.description.data or workflow.description or "",
        "creators": creators,
        "license": form.fair_license.data or "",
        "keywords": split_lines(form.fair_keywords.data or ""),
        "access_policy": form.fair_access_policy.data or "",
        "strict": bool(form.fair_strict.data),
        "capture_environment": bool(form.fair_capture_environment.data),
        "environment_allowlist": split_lines(form.fair_environment_allowlist.data or ""),
    }


def populate_fair_form(form: WorkflowSetupForm, workflow: Workflow) -> None:
    profile = workflow.fair_profile
    form.fair_enabled.data = bool(profile.get("enabled", False))
    form.fair_title.data = profile.get("title") or workflow.name
    form.fair_creators.data = "\n".join(creator.get("name", "") for creator in profile.get("creators", []) if isinstance(creator, dict))
    form.fair_license.data = profile.get("license", "")
    form.fair_keywords.data = ", ".join(profile.get("keywords", []))
    form.fair_access_policy.data = profile.get("access_policy", "")
    form.fair_strict.data = bool(profile.get("strict", False))
    form.fair_capture_environment.data = bool(profile.get("capture_environment", False))
    form.fair_environment_allowlist.data = ", ".join(profile.get("environment_allowlist", []))


def run_file_path(run: WorkflowRun, relative_path: str) -> Path:
    root = scratch_root()
    run_root = Path(run.scratch_path).resolve()
    if root not in run_root.parents and run_root != root:
        raise ValueError("Unsafe run path")
    return safe_child(run_root, relative_path)


def fair_export_root(run: WorkflowRun) -> Path | None:
    if not run.scratch_path:
        return None
    try:
        run_root = run_file_path(run, "")
    except ValueError:
        return None
    direct = [run_root / name for name in FAIR_EXPORT_FILES]
    if any(path.is_file() for path in direct):
        return run_root
    fair_root = run_root / ".dagon" / "fair"
    if fair_root.is_dir():
        candidates = [path for path in fair_root.rglob("run.json") if path.is_file()]
        if candidates:
            return max((path.parent for path in candidates), key=lambda path: path.stat().st_mtime)
    return None


def fair_exports(run: WorkflowRun) -> dict:
    root = fair_export_root(run)
    if not root:
        return {"enabled": bool(run.workflow.fair_profile.get("enabled")), "files": [], "report": None}
    files = []
    for filename, label in FAIR_EXPORT_FILES.items():
        path = root / filename
        if path.is_file():
            files.append({"name": filename, "label": label, "size": path.stat().st_size})
    report = None
    report_path = root / "fairness-report.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    return {"enabled": True, "files": files, "report": report}


def validate_graph_payload(payload: object) -> tuple[list[dict], list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("Graph payload must be an object.")
    task_map = payload.get("tasks")
    if not isinstance(task_map, dict):
        raise ValueError("DAGonStar tasks must be an object keyed by task name.")
    current_workflow_name = payload.get("name", "")
    if not isinstance(current_workflow_name, str):
        current_workflow_name = ""
    tasks: list[dict] = []
    links: list[dict] = []
    for task_name, task_data in task_map.items():
        if not isinstance(task_name, str) or not isinstance(task_data, dict):
            raise ValueError("DAGonStar task entries are invalid.")
        extension = task_data.get("dagonweb", {})
        if not isinstance(extension, dict):
            raise ValueError("DAGonWeb task extension must be an object.")
        task_type = str(task_data.get("type", "batch")).lower()
        task_type = {"bash": "batch", "python": "batch"}.get(task_type, task_type)
        config = extension["config"] if "config" in extension else config_from_document({**task_data, "type": task_type})
        explicit = task_data.get("explicit_prevs", [])
        if not isinstance(explicit, list) or not all(isinstance(item, str) for item in explicit):
            raise ValueError("explicit_prevs must be a list of task names.")
        tasks.append({"name": task_data.get("name", task_name), "task_type": task_type, "x": extension.get("x", 100), "y": extension.get("y", 100), "config": config, "explicit_prevs": explicit})

    names: set[str] = set()
    task_types = {task_type.value for task_type in TaskType}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Each task must be an object.")
        name = task.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("Task names may contain only letters, numbers, underscores, and hyphens.")
        if name in names:
            raise ValueError("Task names must be unique.")
        if task.get("task_type", "batch") not in task_types:
            raise ValueError("Unsupported task type.")
        if not isinstance(task.get("config", {}), dict):
            raise ValueError("Task configuration must be an object.")
        try:
            int(task.get("x", 100))
            int(task.get("y", 100))
            json.dumps(task.get("config", {}))
        except (TypeError, ValueError):
            raise ValueError("Task coordinates and configuration are invalid.") from None
        names.add(name)

    for task in tasks:
        for source in task.get("explicit_prevs", []):
            if source not in names:
                raise ValueError(f"Task {task['name']} has an explicit dependency on unknown task {source}.")
            if source == task["name"]:
                raise ValueError("A task cannot depend on itself.")
            if not any(link["source_name"] == source and link["target_name"] == task["name"] for link in links):
                links.append({"kind": LINK_EXPLICIT, "source_name": source, "source_output": "", "target_name": task["name"], "target_input": ""})

    normalized_links = list(links)
    existing_links = {(link.get("source_name"), link.get("source_output", "output"), link.get("target_name"), link.get("target_input", "input")) for link in links if isinstance(link, dict)}
    existing_edges = {(link.get("source_name"), link.get("target_name")) for link in links if isinstance(link, dict)}
    for task in tasks:
        for workflow_name, source_uid, source_output, target_input in task_workflow_references(task, current_workflow_name):
            if not is_safe_workflow_path(source_output) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", target_input):
                raise ValueError("Workflow reference paths and input names may not contain spaces or unsafe path segments.")
            is_current_workflow = workflow_name in {"", current_workflow_name}
            if not is_current_workflow:
                continue
            if source_uid not in names:
                raise ValueError(f"Task {task['name']} references missing task {source_uid}.")
            if source_uid == task["name"]:
                raise ValueError(f"Task {task['name']} cannot reference its own outputs.")
            link = {"kind": LINK_DATA, "source_name": source_uid, "source_output": source_output, "target_name": task["name"], "target_input": target_input}
            key = (source_uid, source_output, task["name"], target_input)
            if (source_uid, task["name"]) in existing_edges:
                # A data reference supersedes an explicit edge between the same tasks.
                normalized_links = [item for item in normalized_links if not (item.get("kind") == LINK_EXPLICIT and item["source_name"] == source_uid and item["target_name"] == task["name"])]
                existing_edges.discard((source_uid, task["name"]))
            if key not in existing_links and (source_uid, task["name"]) not in existing_edges:
                normalized_links.append(link)
                existing_links.add(key)
                existing_edges.add((source_uid, task["name"]))

    graph = nx.DiGraph()
    graph.add_nodes_from(names)
    pairs: set[tuple[str, str, str, str]] = set()
    for link in normalized_links:
        if not isinstance(link, dict):
            raise ValueError("Each link must be an object.")
        source_uid = link.get("source_name")
        target_uid = link.get("target_name")
        source_output = link.get("source_output", "output")
        target_input = link.get("target_input", "input")
        if source_uid not in names or target_uid not in names:
            raise ValueError("Links must connect existing tasks.")
        if link.get("kind") == LINK_EXPLICIT:
            graph.add_edge(source_uid, target_uid)
            continue
        if not isinstance(source_output, str) or not is_safe_workflow_path(source_output) or not isinstance(target_input, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", target_input):
            raise ValueError("Link output paths and input names may not contain spaces or unsafe path segments.")
        pair = (source_uid, source_output, target_uid, target_input)
        if pair in pairs:
            raise ValueError("Links must be unique.")
        pairs.add(pair)
        graph.add_edge(source_uid, target_uid)
    if not nx.is_directed_acyclic_graph(graph):
        cycle = " → ".join(edge[0] for edge in nx.find_cycle(graph))
        raise ValueError(f"Workflow graph must be acyclic (cycle: {cycle}).")
    return tasks, normalized_links


def workflow_references(value: object, input_name: str = "input") -> list[tuple[str, str, str, str]]:
    references: list[tuple[str, str, str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            references.extend(workflow_references(item, key if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key) else input_name))
    elif isinstance(value, list):
        for item in value:
            references.extend(workflow_references(item, input_name))
    elif isinstance(value, str):
        references.extend((match.group("workflow"), match.group("task"), match.group("path"), input_name) for match in WORKFLOW_REFERENCE.finditer(value))
    return references


def task_workflow_references(task: dict, current_workflow_name: str = "") -> list[tuple[str, str, str, str]]:
    """Return executable workflow references, excluding embedded Native source code."""
    config = task.get("config", {})
    if not isinstance(config, dict):
        return []
    return workflow_references(editor_references(str(task.get("task_type", "")), config))


def apply_workflow_document(workflow: Workflow, document: object, auto_layout: bool = False) -> None:
    tasks, links = validate_graph_payload(document)
    if isinstance(document, dict) and isinstance(document.get("dagonweb"), dict):
        workflow.dagonweb = document["dagonweb"]
    if isinstance(document, dict) and isinstance(document.get("pre_stage"), bool):
        workflow.runtime_options = {**workflow.runtime_options, "pre_stage": document["pre_stage"]}
    if auto_layout:
        apply_import_layout(tasks, links)
    workflow.tasks.clear()
    workflow.links.clear()
    db.session.flush()
    for task in tasks:
        workflow_task = WorkflowTask(workflow_id=workflow.id, uid=task["name"], label=task["name"], task_type=task.get("task_type", "batch"), x=int(task.get("x", 100)), y=int(task.get("y", 100)))
        workflow_task.config = task.get("config", {})
        db.session.add(workflow_task)
    for link in links:
        source_output = link.get("source_output", "output")
        if link.get("kind") == LINK_EXPLICIT:
            db.session.add(WorkflowLink(workflow_id=workflow.id, kind=LINK_EXPLICIT, source_uid=link["source_name"], source_output="", target_uid=link["target_name"], target_input="", workflow_uri=""))
            continue
        db.session.add(WorkflowLink(workflow_id=workflow.id, kind=LINK_DATA, source_uid=link["source_name"], source_output=source_output, target_uid=link["target_name"], target_input=link.get("target_input", "input"), workflow_uri=f"workflow:///{link['source_name']}/{source_output}"))


def apply_import_layout(tasks: list[dict], links: list[dict]) -> None:
    graph = nx.DiGraph()
    graph.add_nodes_from(task["name"] for task in tasks)
    graph.add_edges_from((link["source_name"], link["target_name"]) for link in links)
    levels: dict[str, int] = {}
    for task_name in nx.topological_sort(graph):
        levels[task_name] = max((levels[parent] + 1 for parent in graph.predecessors(task_name)), default=0)
    grouped: dict[int, list[str]] = {}
    for task_name, level in levels.items():
        grouped.setdefault(level, []).append(task_name)
    positions = {task_name: (120 + level * 260, 100 + index * 150) for level, names in grouped.items() for index, task_name in enumerate(sorted(names))}
    for task in tasks:
        task["x"], task["y"] = positions[task["name"]]

@bp.route("/")
@login_required
def list_workflows():
    query = Workflow.query
    if not current_user.has_role("admin"):
        query = query.filter((Workflow.owner_id == current_user.id) | (Workflow.is_public == True))
    workflows = query.order_by(Workflow.updated_at.desc()).all()
    runs = WorkflowRun.query.filter(WorkflowRun.workflow_id.in_([wf.id for wf in workflows]) if workflows else sa_false())
    if not current_user.has_role("admin"):
        runs = runs.filter(WorkflowRun.user_id == current_user.id)
    last_runs: dict[int, WorkflowRun] = {}
    for run in runs.order_by(WorkflowRun.created_at.desc()).all():
        last_runs.setdefault(run.workflow_id, run)
    return render_template(
        "workflows/list.html",
        workflows=workflows,
        last_runs=last_runs,
        template_options=workflow_template_options(),
        template_cards=workflow_template_cards(),
        specs=TASK_SPECS,
        categories=dict(CATEGORIES),
    )

@bp.route("/new", methods=["GET", "POST"])
@login_required
def create_workflow():
    template = request.args.get("template", "").strip()
    if request.method == "GET" and template:
        try:
            template_labels = dict(workflow_template_options())
            if template not in template_labels:
                raise ValueError("Unknown workflow template.")
            label = template_labels[template]
            workflow_name = validate_dagonstar_name(f"New_{template}_workflow", "Workflow name")
            wf = Workflow(owner_id=current_user.id, name=workflow_name, description=f"Started from the {label} template.", is_public=False)
            db.session.add(wf)
            db.session.flush()
            apply_workflow_template(wf, template)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("workflows.list_workflows"))
        flash("Workflow created.", "success")
        return redirect(url_for("workflows.editor", workflow_id=wf.id))
    form = WorkflowForm()
    if form.validate_on_submit():
        wf = Workflow(owner_id=current_user.id, name=validate_dagonstar_name(form.name.data, "Workflow name"), description=form.description.data or "", is_public=form.is_public.data)
        db.session.add(wf)
        db.session.commit()
        flash("Workflow created.", "success")
        return redirect(url_for("workflows.editor", workflow_id=wf.id))
    return render_template("workflows/form.html", form=form, title="New workflow")

@bp.route("/<int:workflow_id>/edit-meta", methods=["GET", "POST"])
@login_required
def edit_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    form = WorkflowForm(obj=wf)
    if form.validate_on_submit():
        wf.name = validate_dagonstar_name(form.name.data, "Workflow name")
        wf.description = form.description.data or ""
        wf.is_public = form.is_public.data
        db.session.commit()
        flash("Workflow updated.", "success")
        return redirect(url_for("workflows.list_workflows"))
    return render_template("workflows/form.html", form=form, title="Edit workflow")


@bp.route("/<int:workflow_id>/setup", methods=["GET", "POST"])
@login_required
def workflow_setup(workflow_id: int):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    config = wf.dagon_ini
    form = WorkflowSetupForm(obj=wf)
    if not form.is_submitted():
        form.batch_threads.data = int(config.get("batch", {}).get("threads", 1) or 1)
        form.dagon_service_route.data = config.get("dagon_service", {}).get("route", "")
        form.dagon_service_use.data = str(config.get("dagon_service", {}).get("use", "False")).lower() in {"1", "true", "yes"}
        form.ftp_pub_ip.data = config.get("ftp_pub", {}).get("ip", "")
        form.dagon_ini_content.data = ""
        options = wf.runtime_options
        form.portable_emulation.data = bool(options.get("portable_emulation", False))
        form.pre_stage.data = bool(options.get("pre_stage", False))
        form.max_threads.data = int(options.get("max_threads") or 10)
        populate_fair_form(form, wf)
    if form.validate_on_submit():
        wf.name = validate_dagonstar_name(form.name.data, "Workflow name")
        wf.description = form.description.data or ""
        wf.is_public = form.is_public.data
        wf.fair_profile = fair_profile_from_form(form, wf)
        wf.runtime_options = {
            "portable_emulation": bool(form.portable_emulation.data),
            "pre_stage": bool(form.pre_stage.data),
            "max_threads": int(form.max_threads.data or 10),
        }
        advanced = (form.dagon_ini_content.data or "").strip()
        if advanced:
            try:
                wf.dagon_ini = parse_dagon_ini(advanced)
            except ConfigParserError as exc:
                form.dagon_ini_content.errors.append(f"Invalid INI configuration: {exc}")
                return render_template("workflows/form.html", form=form, title="Workflow setup", setup_mode=True, workflow=wf, dagon_ini_preview="")
        else:
            # Keep advanced sections (agent profiles, providers) that the
            # guided form does not edit.
            config = {section: values for section, values in wf.dagon_ini.items() if section not in {"batch", "dagon_service", "ftp_pub"}}
            config.update({
                "batch": {"threads": str(form.batch_threads.data or 1)},
                "dagon_service": {"route": form.dagon_service_route.data or "", "use": "True" if form.dagon_service_use.data else "False"},
                "ftp_pub": {"ip": form.ftp_pub_ip.data or ""},
            })
            wf.dagon_ini = config
        db.session.commit()
        flash("Workflow setup saved.", "success")
        return redirect(url_for("workflows.list_workflows"))
    return render_template("workflows/form.html", form=form, title="Workflow setup", setup_mode=True, workflow=wf, dagon_ini_preview=dump_dagon_ini(wf.dagon_ini) if wf.dagon_ini else "")


@bp.post("/<int:workflow_id>/rename")
@login_required
def rename_workflow(workflow_id: int):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    try:
        old_name = wf.name
        new_name = validate_dagonstar_name(request.form.get("name", ""), "Workflow name")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("workflows.list_workflows"))
    if new_name != old_name:
        for task in wf.tasks:
            task.config = rewrite_workflow_reference_names(task.config, old_name, new_name)
        wf.name = new_name
    db.session.commit()
    flash("Workflow renamed.", "success")
    return redirect(url_for("workflows.list_workflows"))


@bp.post("/<int:workflow_id>/delete")
@login_required
def delete_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    if wf.has_active_runs:
        flash("Wait for active runs to finish before deleting this workflow.", "warning")
        return redirect(url_for("workflows.list_workflows"))
    for run in wf.runs:
        remove_run_scratch(run)
    db.session.delete(wf)
    db.session.commit()
    flash("Workflow deleted.", "success")
    return redirect(url_for("workflows.list_workflows"))


@bp.post("/delete-all")
@login_required
def delete_all_workflows():
    query = Workflow.query
    if not current_user.has_role("admin"):
        query = query.filter(Workflow.owner_id == current_user.id)
    workflows = [workflow for workflow in query.all() if not workflow.has_active_runs]
    for workflow in workflows:
        for run in workflow.runs:
            remove_run_scratch(run)
        db.session.delete(workflow)
    db.session.commit()
    flash(f"Deleted {len(workflows)} workflow{'s' if len(workflows) != 1 else ''}.", "success")
    return redirect(url_for("workflows.list_workflows"))


@bp.route("/<int:workflow_id>/editor")
@login_required
def editor(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    return render_template("workflows/editor.html", workflow=wf, editable=can_edit(wf), task_types=TASK_TYPE_LABELS.items(), palette=palette())

@bp.get("/<int:workflow_id>/graph")
@login_required
def graph(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    return jsonify(wf.as_json())

@bp.post("/<int:workflow_id>/graph")
@login_required
def save_graph(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    try:
        apply_workflow_document(wf, request.get_json(force=True))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    db.session.commit()
    return jsonify({"status": "ok", "workflow": wf.as_json()})


@bp.get("/<int:workflow_id>/download")
@login_required
def download_workflow(workflow_id: int):
    workflow = db.get_or_404(Workflow, workflow_id)
    if not can_access(workflow):
        abort(403)
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", workflow.name).strip("-") or f"workflow-{workflow.id}"
    return Response(json.dumps(workflow.as_json(), indent=2), mimetype="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})


@bp.get("/<int:workflow_id>/python")
@login_required
def download_workflow_python(workflow_id: int):
    workflow = db.get_or_404(Workflow, workflow_id)
    if not can_access(workflow):
        abort(403)
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", workflow.name).strip("-") or f"workflow-{workflow.id}"
    source = workflow_python_source(workflow)
    return Response(source, mimetype="text/x-python", headers={"Content-Disposition": f'attachment; filename="{filename}.py"'})


SECRET_KEY_HINTS = ("key", "secret", "token", "password", "credential", "authorization")


def portable_ini_sections(workflow: Workflow) -> dict[str, dict[str, str]]:
    """Agent profile sections safe to embed in a generated program."""
    sections: dict[str, dict[str, str]] = {}
    for section, values in workflow.dagon_ini.items():
        if not isinstance(values, dict) or not (section.startswith("agent.") or section.startswith("agent_provider.")):
            continue
        sections[section] = {str(key): str(value) for key, value in values.items() if not any(hint in str(key).lower() for hint in SECRET_KEY_HINTS)}
    return sections


def workflow_python_source(workflow: Workflow) -> str:
    """Generate an editable, direct DAGonStar Python workflow program."""
    identifiers: dict[str, str] = {}
    used_identifiers: set[str] = set()
    for task in workflow.tasks:
        base = re.sub(r"\W|^(?=\d)", "_", task.name)
        identifier = f"task_{base}"
        suffix = 2
        while identifier in used_identifiers:
            identifier = f"task_{base}_{suffix}"
            suffix += 1
        identifiers[task.name] = identifier
        used_identifiers.add(identifier)

    declarations: list[str] = []
    native_sources: dict[str, str] = {}
    for task in workflow.tasks:
        task_type = task.normalized_task_type
        args, kwargs = dagon_task_arguments(task_type, task.name, task.config)
        rendered = [f"TaskType.{task_type.upper()}", *(repr(arg) for arg in args), *(f"{key}={value!r}" for key, value in kwargs.items())]
        declarations.append(f"{identifiers[task.name]} = DagonTask({', '.join(rendered)})")
        source = task.config.get("source") if task_type == "native" else None
        module = str(task.config.get("callable", "")).partition(":")[0]
        if isinstance(source, str) and source.strip() and module:
            native_sources[module] = source.rstrip() + "\n"

    additions = "\n".join(f"    workflow.add_task({identifiers[task.name]})" for task in workflow.tasks)
    explicit = "\n".join(
        f"    {identifiers[link.target_uid]}.add_dependency_to({identifiers[link.source_uid]})"
        for link in workflow.links
        if link.kind == LINK_EXPLICIT and link.source_uid in identifiers and link.target_uid in identifiers
    )
    options = workflow.runtime_options
    workflow_kwargs = []
    if options.get("portable_emulation"):
        workflow_kwargs.append("portable_emulation=True")
    if options.get("pre_stage"):
        workflow_kwargs.append("pre_stage=True")
    if options.get("max_threads"):
        workflow_kwargs.append(f"max_threads={int(options['max_threads'])}")
    workflow_kwargs.append('checkpoint_file=str(workdir / "checkpoint.json")')
    extra_sections = portable_ini_sections(workflow)
    native_block = ""
    if native_sources:
        native_block = f'''
# Embedded Native task sources, written next to the scratch directory and
# added to sys.path so DAGonStar can import them.
NATIVE_SOURCES = {native_sources!r}


def install_native_sources(workdir: Path) -> None:
    module_root = workdir / "native_modules"
    for module, source in NATIVE_SOURCES.items():
        parts = module.split(".")
        target = module_root.joinpath(*parts[:-1], parts[-1] + ".py")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        for depth in range(1, len(parts)):
            module_root.joinpath(*parts[:depth], "__init__.py").touch()
    sys.path.insert(0, str(module_root))

'''
    return f'''#!/usr/bin/env python3
"""Generated DAGonStar workflow: {workflow.name}.

Generated by DAGonWeb for DAGonStar 2.9. Run it with ``python {workflow.name}.py``;
add ``--resume`` to continue a failed run from its checkpoint.
"""
import sys
from pathlib import Path

from dagon import Workflow
from dagon.config import read_config
from dagon.task import DagonTask, TaskType

EXTRA_SECTIONS = {extra_sections!r}

{native_block}
def local_config(workdir: Path) -> dict:
    """Use ./dagon.ini when present, rooted at ``workdir`` for task scratch."""
    config = read_config("dagon.ini") if Path("dagon.ini").is_file() else {{}}
    for section, values in EXTRA_SECTIONS.items():
        config.setdefault(section, dict(values))
    config.setdefault("batch", {{}}).update({{"scratch_dir_base": str(workdir), "remove_dir": False}})
    config.setdefault("ftp_pub", {{}})
    config.setdefault("dagon_service", {{"use": "False"}})
    return config


def build_workflow(workdir: Path) -> Workflow:
    workdir.mkdir(parents=True, exist_ok=True)
{"    install_native_sources(workdir)" if native_sources else ""}
    workflow = Workflow({workflow.name!r}, config=local_config(workdir), {", ".join(workflow_kwargs)})

    # Task declarations
{chr(10).join('    ' + declaration for declaration in declarations)}

    # Register tasks before DAGonStar discovers workflow:// dependencies.
{additions}
{("    # Explicit (non-data) dependencies, serialized as explicit_prevs." + chr(10) + explicit) if explicit else ""}
    workflow.make_dependencies()
    return workflow


if __name__ == "__main__":
    workdir = Path.cwd() / "scratch"
    workflow = build_workflow(workdir)
    checkpoint = workdir / "checkpoint.json"
    workflow.launch(resume_checkpoint_file=str(checkpoint) if "--resume" in sys.argv and checkpoint.is_file() else None)
    workflow.wait()
    failed = [task.name for task in workflow.tasks if task.status.name != "FINISHED"]
    print("Failed tasks: " + ", ".join(failed) if failed else "Workflow completed.")
    sys.exit(1 if failed else 0)
'''


@bp.get("/<int:workflow_id>/cwl")
@login_required
def download_workflow_cwl(workflow_id: int):
    workflow = db.get_or_404(Workflow, workflow_id)
    if not can_access(workflow):
        abort(403)
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", workflow.name).strip("-") or f"workflow-{workflow.id}"
    try:
        source = export_cwl(workflow)
    except Exception as exc:  # DAGonStar validation errors are user-facing here.
        flash(f"CWL export failed: {exc}", "danger")
        return redirect(url_for("workflows.editor", workflow_id=workflow.id))
    return Response(source, mimetype="application/x-yaml", headers={"Content-Disposition": f'attachment; filename="{filename}.cwl"'})


@bp.post("/upload")
@login_required
def upload_workflow():
    uploaded = request.files.get("workflow_file")
    if not uploaded or not uploaded.filename:
        flash("Choose a workflow JSON file to upload.", "warning")
        return redirect(url_for("workflows.list_workflows"))
    try:
        raw_document = uploaded.read(1_000_001)
        if len(raw_document) > 1_000_000:
            raise ValueError("Workflow file is too large.")
        document = json.loads(raw_document)
        if not isinstance(document, dict) or not isinstance(document.get("tasks"), dict):
            raise ValueError("Unsupported DAGonStar workflow JSON format.")
        name = document.get("name")
        if not isinstance(name, str):
            raise ValueError("Workflow name is invalid.")
        workflow = Workflow(owner_id=current_user.id, name=validate_dagonstar_name(name, "Workflow name"), description=str(document.get("description", "")), is_public=bool(document.get("is_public", False)))
        db.session.add(workflow)
        db.session.flush()
        apply_workflow_document(workflow, document, auto_layout=True)
        db.session.commit()
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        db.session.rollback()
        flash("The uploaded file is not a valid DAGonWeb workflow document.", "danger")
        return redirect(url_for("workflows.list_workflows"))
    flash("Workflow uploaded.", "success")
    return redirect(url_for("workflows.editor", workflow_id=workflow.id))

def start_run_thread(workflow_id: int, run_id: int, resume: bool = False) -> None:
    app = current_app._get_current_object()

    def execute_in_background() -> None:
        with app.app_context():
            try:
                workflow = db.session.get(Workflow, workflow_id)
                workflow_run = db.session.get(WorkflowRun, run_id)
                if workflow and workflow_run:
                    execute_workflow(workflow, workflow_run.user_id, workflow_run, resume=resume)
            except Exception as exc:
                db.session.rollback()
                workflow_run = db.session.get(WorkflowRun, run_id)
                if workflow_run:
                    workflow_run.status = "failed"
                    workflow_run.log = (workflow_run.log or "") + f"Unable to start DAGonStar workflow: {exc}\n"
                    db.session.commit()

    Thread(target=execute_in_background, daemon=False, name=f"DAGonWeb-run-{run_id}").start()


@bp.post("/<int:workflow_id>/run")
@login_required
def run_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    if not wf.tasks:
        flash("Add at least one task before running the workflow.", "warning")
        return redirect(url_for("workflows.editor", workflow_id=wf.id))
    run = WorkflowRun(workflow_id=wf.id, user_id=current_user.id, status="pending", label=(request.form.get("label") or "").strip()[:255], scratch_path="", log="")
    db.session.add(run)
    db.session.commit()
    start_run_thread(wf.id, run.id)
    flash("Workflow run started.", "info")
    return redirect(url_for("workflows.run_detail", workflow_id=wf.id, run_id=run.id))


@bp.post("/runs/<int:run_id>/resume")
@login_required
def resume_run(run_id: int):
    run = db.get_or_404(WorkflowRun, run_id)
    if not can_manage_run(run):
        abort(403)
    if run.status != "failed":
        flash("Only failed runs can be resumed.", "warning")
        return redirect(url_for("workflows.run_detail", workflow_id=run.workflow_id, run_id=run.id))
    run.status = "pending"
    db.session.commit()
    start_run_thread(run.workflow_id, run.id, resume=True)
    flash("Resuming the run: completed tasks are reused from the DAGonStar checkpoint.", "info")
    return redirect(url_for("workflows.run_detail", workflow_id=run.workflow_id, run_id=run.id))


@bp.get("/runs")
@login_required
def list_runs():
    query = WorkflowRun.query.join(Workflow)
    if not current_user.has_role("admin"):
        query = query.filter(WorkflowRun.user_id == current_user.id)
    return render_template("workflows/runs.html", runs=query.order_by(WorkflowRun.created_at.desc()).all())


@bp.route("/runs/<int:run_id>/edit", methods=["GET", "POST"])
@login_required
def edit_run(run_id: int):
    run = db.get_or_404(WorkflowRun, run_id)
    if not can_manage_run(run):
        abort(403)
    form = RunForm(obj=run)
    if form.validate_on_submit():
        run.label = (form.label.data or "").strip()
        db.session.commit()
        flash("Run updated.", "success")
        return redirect(url_for("workflows.run_detail", workflow_id=run.workflow_id, run_id=run.id))
    return render_template("workflows/run_form.html", form=form, run=run)


@bp.post("/runs/<int:run_id>/delete")
@login_required
def delete_run(run_id: int):
    run = db.get_or_404(WorkflowRun, run_id)
    if not can_manage_run(run):
        abort(403)
    if run.status in ACTIVE_RUN_STATUSES:
        flash("A run cannot be deleted while it is active.", "warning")
        return redirect(url_for("workflows.run_detail", workflow_id=run.workflow_id, run_id=run.id))
    remove_run_scratch(run)
    db.session.delete(run)
    db.session.commit()
    flash("Run and its scratch files deleted.", "success")
    return redirect(url_for("workflows.list_runs"))

@bp.route("/<int:workflow_id>/runs/<int:run_id>")
@login_required
def run_detail(workflow_id, run_id):
    wf = db.get_or_404(Workflow, workflow_id)
    run = next((r for r in wf.runs if r.id == run_id), None)
    if not run:
        abort(404)
    if not can_access_run(run):
        abort(403)
    specs = {key: {"label": spec.label, "icon": spec.icon, "category": spec.category} for key, spec in TASK_SPECS.items()}
    return render_template("workflows/run.html", workflow=wf, run=run, fair=fair_exports(run), specs=specs, can_manage=can_manage_run(run))


@bp.get("/<int:workflow_id>/runs/<int:run_id>/fair/<path:filename>")
@login_required
def download_fair_export(workflow_id: int, run_id: int, filename: str):
    run = db.get_or_404(WorkflowRun, run_id)
    if run.workflow_id != workflow_id:
        abort(404)
    if not can_access_run(run):
        abort(403)
    if filename not in FAIR_EXPORT_FILES:
        abort(404)
    root = fair_export_root(run)
    if not root:
        abort(404)
    try:
        path = safe_child(root, filename)
    except ValueError:
        abort(403)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


@bp.get("/<int:workflow_id>/runs/<int:run_id>/status")
@login_required
def run_status(workflow_id: int, run_id: int):
    run = db.get_or_404(WorkflowRun, run_id)
    if run.workflow_id != workflow_id:
        abort(404)
    if not can_access_run(run):
        abort(403)
    return jsonify({
        "status": run.status,
        "log": run.log or "",
        "created_at": run.created_at.isoformat() + "Z",
        "updated_at": run.updated_at.isoformat() + "Z",
        "can_resume": run.status == "failed" and can_manage_run(run),
        "tasks": [
            {
                "id": task_run.id,
                "name": task_run.task_uid,
                "status": task_run.status,
                "scratch_path": task_run.scratch_path,
                "log": task_run.log or "",
                "updated_at": task_run.updated_at.isoformat() + "Z",
            }
            for task_run in run.task_runs
        ],
    })

@bp.get("/files")
@login_required
def files():
    path = request.args.get("path", "")
    task_run = TaskRun.query.filter_by(scratch_path=path).first()
    if not task_run or not can_access_run(task_run.run):
        abort(403)
    try:
        return jsonify(list_files(path))
    except ValueError:
        abort(403)


def task_run_file_path(task_run: TaskRun, relative_path: str) -> Path:
    # Stored paths are treated as untrusted too: never let a record point the
    # browser outside the configured scratch root.
    root = scratch_root()
    task_root = Path(task_run.scratch_path).resolve()
    if root not in task_root.parents and task_root != root:
        raise ValueError("Unsafe scratch path")
    return safe_child(task_root, relative_path)


@bp.get("/task-runs/<int:task_run_id>/files")
@login_required
def browse_task_run_files(task_run_id: int):
    task_run = db.get_or_404(TaskRun, task_run_id)
    if not can_access_run(task_run.run):
        abort(403)
    relative_path = request.args.get("path", "")
    try:
        base = task_run_file_path(task_run, relative_path)
        return jsonify({"path": relative_path, "files": list_directory(base)})
    except ValueError:
        abort(403)


@bp.get("/task-runs/<int:task_run_id>/file")
@login_required
def preview_task_run_file(task_run_id: int):
    task_run = db.get_or_404(TaskRun, task_run_id)
    if not can_access_run(task_run.run):
        abort(403)
    relative_path = request.args.get("path", "")
    try:
        return jsonify({"path": relative_path, **preview_task_file(task_run_file_path(task_run, relative_path))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.get("/task-runs/<int:task_run_id>/download")
@login_required
def download_task_run_file(task_run_id: int):
    task_run = db.get_or_404(TaskRun, task_run_id)
    if not can_access_run(task_run.run):
        abort(403)
    try:
        path = task_run_file_path(task_run, request.args.get("path", ""))
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)
    except ValueError:
        abort(403)


@bp.get("/task-runs/<int:task_run_id>/archive")
@login_required
def download_task_run_archive(task_run_id: int):
    task_run = db.get_or_404(TaskRun, task_run_id)
    if not can_access_run(task_run.run):
        abort(403)
    try:
        root = task_run_file_path(task_run, "")
        if not root.is_dir():
            abort(404)
        archive = BytesIO()
        with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
            for path in root.rglob("*"):
                if path.is_file():
                    zip_file.write(path, path.relative_to(root))
        archive.seek(0)
        return send_file(archive, mimetype="application/zip", as_attachment=True, download_name=f"{task_run.task_uid}-scratch.zip")
    except ValueError:
        abort(403)
