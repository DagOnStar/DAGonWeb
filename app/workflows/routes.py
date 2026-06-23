from __future__ import annotations

import json
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from ..extensions import db
from ..models import Workflow, WorkflowLink, WorkflowTask
from ..executor.local import execute_workflow, list_files
from .forms import WorkflowForm

bp = Blueprint("workflows", __name__)

def can_access(workflow: Workflow) -> bool:
    return workflow.owner_id == current_user.id or current_user.has_role("admin") or workflow.is_public

def can_edit(workflow: Workflow) -> bool:
    return workflow.owner_id == current_user.id or current_user.has_role("admin")

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
    return render_template("workflows/editor.html", workflow=wf, editable=can_edit(wf))

@bp.get("/<int:workflow_id>/graph")
@login_required
def graph(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    return jsonify(wf.to_graph_json())

@bp.post("/<int:workflow_id>/graph")
@login_required
def save_graph(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_edit(wf):
        abort(403)
    payload = request.get_json(force=True)
    wf.tasks.clear()
    wf.links.clear()
    db.session.flush()
    for task in payload.get("tasks", []):
        wt = WorkflowTask(workflow_id=wf.id, uid=task["uid"], label=task.get("label") or task["uid"], task_type=task.get("task_type", "bash"), x=int(task.get("x", 100)), y=int(task.get("y", 100)))
        wt.config = task.get("config", {})
        db.session.add(wt)
    for link in payload.get("links", []):
        source_output = link.get("source_output", "output")
        uri = f"workflow://{link['source_uid']}/{source_output}"
        db.session.add(WorkflowLink(workflow_id=wf.id, source_uid=link["source_uid"], source_output=source_output, target_uid=link["target_uid"], target_input=link.get("target_input", "input"), workflow_uri=uri))
    db.session.commit()
    return jsonify({"status": "ok", "workflow": wf.to_graph_json()})

@bp.post("/<int:workflow_id>/run")
@login_required
def run_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    run = execute_workflow(wf, current_user.id)
    flash(f"Workflow run finished with status: {run.status}", "info")
    return redirect(url_for("workflows.run_detail", workflow_id=wf.id, run_id=run.id))

@bp.route("/<int:workflow_id>/runs/<int:run_id>")
@login_required
def run_detail(workflow_id, run_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    run = next((r for r in wf.runs if r.id == run_id), None)
    if not run:
        abort(404)
    return render_template("workflows/run.html", workflow=wf, run=run)

@bp.get("/files")
@login_required
def files():
    path = request.args.get("path", "")
    try:
        return jsonify(list_files(path))
    except ValueError:
        abort(403)
