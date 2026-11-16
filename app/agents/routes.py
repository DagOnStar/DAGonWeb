"""HTTP API of the agentic workflow assistant."""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request
from flask_login import current_user

from .. import audit
from ..authz import permission_required
from ..extensions import db
from ..models import Workflow
from .assistant import run_assistant
from .llm import LLMError, is_enabled
from .tools import Draft, validate_workflow

bp = Blueprint("assistant", __name__)


def _editable_workflow(workflow_id: int) -> Workflow:
    workflow = db.get_or_404(Workflow, workflow_id)
    if not (workflow.owner_id == current_user.id or current_user.is_admin):
        abort(403)
    return workflow


def _draft(workflow: Workflow, document: object) -> Draft:
    source = document if isinstance(document, dict) and isinstance(document.get("tasks"), dict) else workflow.as_json()
    draft = Draft.from_document(source, workflow_id=workflow.id, user_id=current_user.id, is_admin=current_user.is_admin)
    draft.name = workflow.name
    return draft


@bp.get("/status")
@permission_required("assistant.use")
def status():
    return jsonify({"enabled": is_enabled()})


@bp.post("/workflows/<int:workflow_id>/chat")
@permission_required("assistant.use")
def chat(workflow_id: int):
    workflow = _editable_workflow(workflow_id)
    if not is_enabled():
        return jsonify({"error": "The workflow assistant is not configured. An administrator can enable it under Admin → LLM connector."}), 503
    payload = request.get_json(silent=True) or {}
    draft = _draft(workflow, payload.get("draft"))
    try:
        result = run_assistant(draft, payload.get("messages"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except LLMError as exc:
        return jsonify({"error": f"The language model failed: {exc}"}), 502
    audit.commit("assistant.chat", workflow.name, f"{len(result['trace'])} tool call(s)")
    return jsonify({**result, "draft": draft.document(), "check": validate_workflow(draft) if result["changed"] else None})


@bp.post("/workflows/<int:workflow_id>/validate")
@permission_required("assistant.use")
def validate(workflow_id: int):
    """Dry-run the saved workflow (or a posted draft) without an LLM."""
    workflow = _editable_workflow(workflow_id)
    payload = request.get_json(silent=True) or {}
    return jsonify(validate_workflow(_draft(workflow, payload.get("draft"))))
