from __future__ import annotations

import json
import re
import shutil
from io import BytesIO
from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile
from pathlib import Path
import networkx as nx
from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from ..extensions import db
from ..models import TASK_TYPE_LABELS, TaskRun, TaskType, Workflow, WorkflowLink, WorkflowTask, WorkflowRun
from ..executor.local import execute_workflow, list_directory, list_files, preview_task_file, safe_child, scratch_root
from .forms import RunForm, WorkflowForm

bp = Blueprint("workflows", __name__)
WORKFLOW_REFERENCE = re.compile(r"workflow:///([A-Za-z0-9_-]{1,80})/([^\s]+)")

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


def validate_graph_payload(payload: object) -> tuple[list[dict], list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("Graph payload must be an object.")
    task_map = payload.get("tasks")
    if not isinstance(task_map, dict):
        raise ValueError("DAGonStar tasks must be an object keyed by task name.")
    tasks: list[dict] = []
    links: list[dict] = []
    for task_name, task_data in task_map.items():
        if not isinstance(task_name, str) or not isinstance(task_data, dict):
            raise ValueError("DAGonStar task entries are invalid.")
        extension = task_data.get("dagonweb", {})
        if not isinstance(extension, dict):
            raise ValueError("DAGonWeb task extension must be an object.")
        config = extension.get("config", {"command": task_data.get("command", "")})
        tasks.append({"name": task_data.get("name", task_name), "task_type": task_data.get("type", "batch"), "x": extension.get("x", 100), "y": extension.get("y", 100), "config": config})

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

    normalized_links = list(links)
    existing_links = {(link.get("source_name"), link.get("source_output", "output"), link.get("target_name"), link.get("target_input", "input")) for link in links if isinstance(link, dict)}
    existing_edges = {(link.get("source_name"), link.get("target_name")) for link in links if isinstance(link, dict)}
    for task in tasks:
        for source_uid, source_output, target_input in workflow_references(task.get("config", {})):
            if source_uid not in names:
                raise ValueError("Workflow reference points to a missing task.")
            link = {"source_name": source_uid, "source_output": source_output, "target_name": task["name"], "target_input": target_input}
            key = (source_uid, source_output, task["name"], target_input)
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
        if not isinstance(source_output, str) or not re.fullmatch(r"[^\s]{1,255}", source_output) or not isinstance(target_input, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", target_input):
            raise ValueError("Link output paths and input names are invalid.")
        pair = (source_uid, source_output, target_uid, target_input)
        if pair in pairs:
            raise ValueError("Links must be unique.")
        pairs.add(pair)
        graph.add_edge(source_uid, target_uid)
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("Workflow graph must be acyclic.")
    return tasks, normalized_links


def workflow_references(value: object, input_name: str = "input") -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            references.extend(workflow_references(item, key if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key) else input_name))
    elif isinstance(value, list):
        for item in value:
            references.extend(workflow_references(item, input_name))
    elif isinstance(value, str):
        references.extend((match.group(1), match.group(2), input_name) for match in WORKFLOW_REFERENCE.finditer(value))
    return references


def apply_workflow_document(workflow: Workflow, document: object, auto_layout: bool = False) -> None:
    tasks, links = validate_graph_payload(document)
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
        db.session.add(WorkflowLink(workflow_id=workflow.id, source_uid=link["source_name"], source_output=source_output, target_uid=link["target_name"], target_input=link.get("target_input", "input"), workflow_uri=f"workflow:///{link['source_name']}/{source_output}"))


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
    return render_template("workflows/list.html", workflows=query.order_by(Workflow.updated_at.desc()).all())

@bp.route("/new", methods=["GET", "POST"])
@login_required
def create_workflow():
    form = WorkflowForm()
    if form.validate_on_submit():
        wf = Workflow(owner_id=current_user.id, name=form.name.data, description=form.description.data or "", is_public=form.is_public.data)
        db.session.add(wf)
        db.session.commit()
        flash("Workflow created.", "success")
        return redirect(url_for("workflows.editor", workflow_id=wf.id))
    return render_template("workflows/form.html", form=form)

@bp.route("/<int:workflow_id>/edit-meta", methods=["GET", "POST"])
@login_required
def edit_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    form = WorkflowForm(obj=wf)
    if form.validate_on_submit():
        wf.name = form.name.data
        wf.description = form.description.data or ""
        wf.is_public = form.is_public.data
        db.session.commit()
        flash("Workflow updated.", "success")
        return redirect(url_for("workflows.list_workflows"))
    return render_template("workflows/form.html", form=form)

@bp.post("/<int:workflow_id>/delete")
@login_required
def delete_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    db.session.delete(wf)
    db.session.commit()
    flash("Workflow deleted.", "success")
    return redirect(url_for("workflows.list_workflows"))

@bp.route("/<int:workflow_id>/editor")
@login_required
def editor(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    return render_template("workflows/editor.html", workflow=wf, editable=can_edit(wf), task_types=TASK_TYPE_LABELS.items())

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
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid workflow graph."}), 400
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


def workflow_python_source(workflow: Workflow) -> str:
    """Generate an editable, direct DAGonStar Python workflow program."""
    identifiers: dict[str, str] = {}
    used_identifiers: set[str] = set()
    for task in workflow.tasks:
        base = re.sub(r"\W|^(?=\d)", "_", task.name)
        identifier = f"task_{base}" or "task"
        suffix = 2
        while identifier in used_identifiers:
            identifier = f"task_{base}_{suffix}"
            suffix += 1
        identifiers[task.name] = identifier
        used_identifiers.add(identifier)

    declarations: list[str] = []
    for task in workflow.tasks:
        config = task.config
        task_type = task.normalized_task_type.upper()
        identifier = identifiers[task.name]
        if task_type == "NATIVE":
            options = {key: config[key] for key in ("executor", "resources", "python", "environment") if config.get(key) not in (None, "", {})}
            declarations.append(f"{identifier} = DagonTask(TaskType.NATIVE, {task.name!r}, {config.get('callable', '')!r}, inputs={config.get('inputs', {})!r}, outputs={config.get('outputs', {})!r}, **{options!r})")
        elif task_type == "WEB":
            specification = {key: value for key, value in config.items() if key != "inputs"}
            declarations.append(f"{identifier} = DagonTask(TaskType.WEB, {task.name!r}, {specification!r})")
        elif task_type == "LLM":
            declarations.append(f"{identifier} = DagonTask(TaskType.LLM, {task.name!r}, {config.get('prompt', '')!r}, provider={config.get('provider')!r})")
        else:
            declarations.append(f"{identifier} = DagonTask(TaskType.{task_type}, {task.name!r}, {config.get('command', '')!r})")

    additions = "\n".join(f"    workflow.add_task({identifiers[task.name]})" for task in workflow.tasks)
    return f'''#!/usr/bin/env python3
"""Generated DAGonStar workflow: {workflow.name}."""
from pathlib import Path
from dagon import Workflow
from dagon.task import DagonTask, TaskType


def local_config(workdir: Path) -> dict:
    """Return a local DAGonStar configuration rooted at ``workdir``."""
    return {{
        "batch": {{"scratch_dir_base": str(workdir), "remove_dir": False}},
        "ftp_pub": {{}},
        "dagon_service": {{"use": "False"}},
    }}


def build_workflow(workdir: Path) -> Workflow:
    # DAGonStar derives dependencies from workflow:/// references in commands.
    workflow = Workflow({workflow.name!r}, config=local_config(workdir))

    # Task declarations
{chr(10).join('    ' + declaration for declaration in declarations)}

    # Register tasks before DAGonStar discovers workflow:// dependencies.
{additions}
    workflow.make_dependencies()
    return workflow


if __name__ == "__main__":
    workflow = build_workflow(Path.cwd() / "scratch")
    workflow.run()
'''


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
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise ValueError("Workflow name is invalid.")
        workflow = Workflow(owner_id=current_user.id, name=name.strip(), description=str(document.get("description", "")), is_public=bool(document.get("is_public", False)))
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

@bp.post("/<int:workflow_id>/run")
@login_required
def run_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    run = WorkflowRun(workflow_id=wf.id, user_id=current_user.id, status="pending", label="", scratch_path="", log="")
    db.session.add(run)
    db.session.commit()
    workflow_id = wf.id
    run_id = run.id
    app = current_app._get_current_object()
    def execute_in_background() -> None:
        with app.app_context():
            try:
                workflow = db.session.get(Workflow, workflow_id)
                workflow_run = db.session.get(WorkflowRun, run_id)
                if workflow and workflow_run:
                    execute_workflow(workflow, workflow_run.user_id, workflow_run)
            except Exception as exc:
                workflow_run = db.session.get(WorkflowRun, run_id)
                if workflow_run:
                    workflow_run.status = "failed"
                    workflow_run.log = f"Unable to start DAGonStar workflow: {exc}\n"
                    db.session.commit()
    Thread(target=execute_in_background, daemon=False).start()
    flash("Workflow run started.", "info")
    return redirect(url_for("workflows.run_detail", workflow_id=wf.id, run_id=run.id))


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
    if run.status in {"pending", "running"}:
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
    return render_template("workflows/run.html", workflow=wf, run=run)


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
        "tasks": [
            {
                "id": task_run.id,
                "name": task_run.task_uid,
                "status": task_run.status,
                "scratch_path": task_run.scratch_path,
                "log": task_run.log or "",
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
