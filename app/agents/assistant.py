"""Agentic workflow assistant: an LLM tool-calling loop over the draft tools."""
from __future__ import annotations

import json
from typing import Any

from .. import settings
from .llm import LLMError, chat
from .tools import Draft, call_tool, tool_schemas

MAX_HISTORY = 20
MAX_MESSAGE_CHARS = 8000
MAX_TOOL_RESULT_CHARS = 12000

SYSTEM_PROMPT = """You are the DAGonWeb workflow assistant. You design, implement and test DAGonStar workflows together with the user.

How DAGonStar workflows work:
- A workflow is a DAG of tasks. Each task runs in its own working directory and writes its files there (usually under outputs/).
- Data dependencies are created ONLY by references of the form workflow:///<producer_task>/<relative/path> placed in a task's configuration. DAGonStar stages the producer file and makes the consumer wait.
- For shell-like tasks (batch, slurm, docker, kubernetes, apptainer, nomad, cloud) put references in config.inputs as name = workflow:///task/path; the command then uses $name for the staged path.
- explicit_prevs lists tasks that must finish first without data staging.
- Task names use letters, digits, _ and -. Commands are bash scripts.
- DAGonWeb interactive types (they run as DAGonStar batch tasks): upload (user uploads files; config.save_as names the file under outputs/), form (user fills config.fields; writes outputs/form.json and outputs/form.env), table and chart (config.inputs.source = workflow:///task/file.csv|json; write outputs/table.json or outputs/chart.json and chart.svg), download (config.inputs maps names to files to zip into outputs/<archive>).
- Prefer batch tasks with portable shell and python3 commands unless the user asks for a specific back-end.

How to work:
1. If you need details, call list_task_types or get_workflow.
2. Change the draft with set_workflow (new design) or upsert_task/remove_task (edits).
3. Always call validate_workflow and fix every problem before you answer.
4. When the user reports a failed run, call diagnose_last_run and fix the draft.
Answer briefly in the user's language: summarize what the draft does and what changed. The user applies the draft to the canvas and runs it; never claim it was run."""


def _clean_history(messages: Any) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    if not isinstance(messages, list):
        return history
    for item in messages[-MAX_HISTORY:]:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
            history.append({"role": item["role"], "content": item["content"][:MAX_MESSAGE_CHARS]})
    return history


def run_assistant(draft: Draft, messages: Any) -> dict[str, Any]:
    """Run the tool-calling loop for the latest user message.

    Returns the assistant reply, a trace of tool calls, and whether the draft
    changed. Raises ``LLMError`` when the provider fails.
    """
    history = _clean_history(messages)
    if not history or history[-1]["role"] != "user":
        raise ValueError("Send a user message.")
    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Current draft:\n" + json.dumps({"name": draft.name, "tasks": [{k: t[k] for k in ("name", "task_type", "config", "explicit_prevs")} for t in draft.tasks]})[:MAX_TOOL_RESULT_CHARS]},
        *history,
    ]
    trace: list[dict[str, Any]] = []
    max_steps = int(settings.get("llm_max_steps") or 8)
    tools = tool_schemas()
    for _step in range(max_steps):
        message = chat(conversation, tools=tools)
        calls = message.get("tool_calls") or []
        if not calls:
            return {"reply": str(message.get("content") or "").strip() or "Done.", "trace": trace, "changed": draft.changed}
        conversation.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            result = call_tool(draft, name, function.get("arguments"))
            trace.append({"tool": name, "ok": bool(result.get("ok", True)), "summary": _summary(name, result)})
            conversation.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(result, default=str)[:MAX_TOOL_RESULT_CHARS]})
    # Out of steps: ask for a final answer without tools.
    conversation.append({"role": "system", "content": "Step limit reached. Summarize the current draft and any open problems now, without calling tools."})
    message = chat(conversation)
    return {"reply": str(message.get("content") or "").strip() or "I reached the step limit.", "trace": trace, "changed": draft.changed}


def _summary(name: str, result: dict[str, Any]) -> str:
    if result.get("error"):
        return str(result["error"])[:300]
    if name == "validate_workflow":
        problems = result.get("problems") or []
        return f"{len(problems)} problem(s)" + (": " + "; ".join(problems)[:300] if problems else "; " + str(result.get("dagonstar", "")))
    if name in ("set_workflow", "upsert_task", "remove_task"):
        return f"{result.get('tasks', 0)} task(s), {len(result.get('dependencies') or [])} dependencies"
    if name == "diagnose_last_run":
        return f"run {result.get('run_id')}: {result.get('status')}"
    return "ok"


__all__ = ["LLMError", "run_assistant", "SYSTEM_PROMPT"]
