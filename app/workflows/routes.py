from __future__ import annotations

import json
import posixpath
import re
import shutil
from configparser import Error as ConfigParserError
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from pathlib import Path
import networkx as nx
from sqlalchemy import false as sa_false
from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from ..extensions import db
from ..dagon_ini import dump_dagon_ini, parse_dagon_ini
from ..models import ACTIVE_RUN_STATUSES, LINK_DATA, LINK_EXPLICIT, TaskRun, TaskType, Workflow, WorkflowLink, WorkflowTask, WorkflowRun
from ..executor.local import timestamped_log, execute_workflow, export_cwl, is_geojson, list_directory, list_files, preview_task_file, safe_child, scratch_root
from ..task_specs import INTERACTIVE_TASK_TYPES, CATEGORIES, TASK_SPECS, TASK_TYPE_LABELS, WORKFLOW_REFERENCE, check_volume_provider, config_from_document, dagon_task_arguments, editor_references, palette
from werkzeug.utils import secure_filename
from ..interactive import coerce_form_values, form_document, form_env, form_fields, safe_filename
from .forms import DAGONSTAR_NAME_RE, RunForm, WorkflowForm, WorkflowSetupForm
from .. import audit, hpc, settings
from ..authz import permission_required
from ..pycode import PythonImportError, import_python_files, portable_ini_sections, python_bundle, read_upload, workflow_python_source  # noqa: F401

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
    "volume": "A shared DAGonFS volume: two writers, a stream between tasks, and a destroy task that stages results out.",
    "meta": "Batch → Meta (an external word-count engine run unmodified) → Batch.",
    "meta_emulated": "Batch → Meta (a Nextflow word count) → Batch. The mock engine stands in for Nextflow, so no engine is needed.",
    "upload": "Upload a CSV on the run page, then see it as a table and a chart.",
    "form": "Fill a form on the run page; a Batch task reads the values from form.env.",
    "table": "A Batch task writes a CSV that the run page shows as a table.",
    "chart": "A Batch task writes a CSV that the run page plots as a line chart.",
    "download": "Produce a CSV and a table, then offer both as one ZIP download.",
}
# Templates for external back-ends enable DAGonStar portable emulation, so the
# example runs locally until the real back-end is configured.
EMULATED_TEMPLATES = {"slurm", "cloud", "docker", "kubernetes", "apptainer", "nomad", "llm", "volume", "meta_emulated"}
# Templates that are not named after a task type: key → (label, task type used for the card icon).
EXTRA_TEMPLATES = {"meta_emulated": ("Meta (emulated Nextflow)", "meta")}
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


MEASUREMENTS_COMMAND = (
    "mkdir -p outputs\n"
    "cat > outputs/measurements.csv <<'CSV'\n"
    "month,rainfall_mm,temperature_c\n"
    "Jan,98,10.4\nFeb,82,11.0\nMar,74,13.1\nApr,66,15.6\nMay,42,19.8\nJun,19,24.0\n"
    "CSV\n"
    "wc -l outputs/measurements.csv"
)


# Meta template: a stand-in external engine with no DAGonStar dependency. It
# reads the parameter document of DAGonStar's ``command`` adapter.
NEXTFLOW_WORDCOUNT = (
    "// Word count in Nextflow. Under portable emulation DAGonStar's mock engine\n"
    "// stands in for Nextflow and writes a placeholder for each declared output.\n"
    "params.text = null\n"
    "params.top = 10\n"
    "\n"
    "process COUNT {\n"
    "    publishDir 'results', mode: 'copy'\n"
    "\n"
    "    input:\n"
    "    path text\n"
    "\n"
    "    output:\n"
    "    path 'counts.tsv'\n"
    "\n"
    "    script:\n"
    "    \"\"\"\n"
    "    tr -s '[:space:]' '\\\\n' < $text | sort | uniq -c | sort -rn | head -n ${params.top} | awk '{print \\$2 \"\\\\t\" \\$1}' > counts.tsv\n"
    "    \"\"\"\n"
    "}\n"
    "\n"
    "workflow {\n"
    "    COUNT(file(params.text))\n"
    "}\n"
)
WORDCOUNT_ENGINE = (
    "\"\"\"Stand-in external engine: count the words of `text`.\"\"\"\n"
    "import json\n"
    "import os\n"
    "import sys\n"
    "from collections import Counter\n"
    "\n"
    "parameters = json.load(open(sys.argv[1], encoding=\"utf-8\"))\n"
    "words = open(parameters[\"text\"], encoding=\"utf-8\").read().lower().split()\n"
    "os.makedirs(\"results\", exist_ok=True)\n"
    "with open(\"results/counts.tsv\", \"w\", encoding=\"utf-8\") as stream:\n"
    "    for word, count in Counter(words).most_common(parameters.get(\"top\", 10)):\n"
    "        stream.write(f\"{word}\\t{count}\\n\")\n"
)
VOLUME_EMIT_COMMAND = (
    "for i in $(cat workflow:///scratch/shared/inputs/data/n.txt ); do\n"
    "  echo $i > item-$i\n"
    "  \"$DAGON_PYTHON\" -m dagon.adhoc stream publish \"$DAGON_STREAM_OUT\" item-$i\n"
    "done"
)
VOLUME_TOTAL_COMMAND = (
    "total=0\n"
    "for item in $(\"$DAGON_PYTHON\" -m dagon.adhoc stream items workflow:///emit/numbers?stream=shared --into got); do\n"
    "  total=$((total + $(cat \"$item\")))\n"
    "done\n"
    "echo $total > sum.txt"
)


def workflow_template_options() -> list[tuple[str, str]]:
    options = [("batch", "Batch diamond")]
    for task_type, label in TASK_TYPE_LABELS.items():
        if task_type != "batch":
            options.append((task_type, label))
        options.extend((key, extra[0]) for key, extra in EXTRA_TEMPLATES.items() if extra[1] == task_type)
    return options


def workflow_template_cards() -> list[dict]:
    cards = []
    for key, label in workflow_template_options():
        spec = TASK_SPECS[EXTRA_TEMPLATES[key][1] if key in EXTRA_TEMPLATES else key]
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
    if template not in TASK_SPECS and template not in EXTRA_TEMPLATES:
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
    elif template == "volume":
        add_workflow_task(workflow, "prepare", "batch", 80, 220, {"command": "mkdir -p data results\nseq 1 5 > data/n.txt\ntouch results/.keep", "inputs": {}})
        add_workflow_task(workflow, "scratch", "volume", 320, 220, {
            "action": "create", "provider": "dagonfs", "volume": "shared", "release": "auto", "options": {},
            "stage_in": {"inputs/data": "workflow:///prepare/data", "results": "workflow:///prepare/results"},
        })
        add_workflow_task(workflow, "square", "batch", 580, 80, {"command": "for i in $(cat workflow:///scratch/shared/inputs/data/n.txt ); do echo $((i * i)); done > workflow:///scratch/shared/results/squares.txt", "inputs": {}})
        add_workflow_task(workflow, "emit", "batch", 580, 240, {"command": VOLUME_EMIT_COMMAND, "inputs": {}})
        add_workflow_task(workflow, "total", "batch", 820, 320, {"command": VOLUME_TOTAL_COMMAND, "inputs": {}})
        add_workflow_task(workflow, "release", "volume", 1060, 160, {
            "action": "destroy", "volume": "workflow:///scratch/shared", "release": "auto",
            "stage_out": {"results": {"path": "results", "type": "directory"}},
        })
        add_workflow_task(workflow, "report", "batch", 1300, 220, {"command": "cat workflow:///release/results/squares.txt workflow:///total/sum.txt > report.txt", "inputs": {}})
        workflow.runtime_options = {**workflow.runtime_options, "portable_emulation": True}
    elif template == "meta":
        add_workflow_task(workflow, "prepare", "batch", 120, 200, {"command": "mkdir -p data\nprintf 'sea wind sea wave sea wind\\n' > data/text.txt", "inputs": {}})
        add_workflow_task(workflow, "wordcount", "meta", 420, 200, {
            "engine": "command", "workflow": "wordcount.py", "workflow_source": WORDCOUNT_ENGINE,
            "inputs": {"text": "workflow:///prepare/data/text.txt", "top": 3},
            "outputs": {"counts": {"path": "counts.tsv", "source": "results/counts.tsv"}},
            "engine_options": {"command": ["{python}", "{workflow}", "{parameters_file}"]},
            "environment": {}, "staging": "copy",
        })
        add_workflow_task(workflow, "report", "batch", 720, 200, {"command": "head -n 1 workflow:///wordcount/outputs/counts.tsv > top.txt", "inputs": {}})
    elif template == "meta_emulated":
        add_workflow_task(workflow, "prepare", "batch", 120, 200, {"command": "mkdir -p data\nprintf 'sea wind sea wave sea wind\\n' > data/text.txt", "inputs": {}})
        add_workflow_task(workflow, "wordcount", "meta", 420, 200, {
            "engine": "nextflow", "workflow": "main.nf", "workflow_source": NEXTFLOW_WORDCOUNT,
            "inputs": {"text": "workflow:///prepare/data/text.txt", "top": 3},
            "outputs": {"counts": {"path": "counts.tsv", "source": "results/counts.tsv"}},
            "engine_options": {"profile": "standard"},
            "environment": {}, "staging": "copy",
        })
        add_workflow_task(workflow, "report", "batch", 720, 200, {"command": "cp workflow:///wordcount/outputs/counts.tsv report.txt", "inputs": {}})
        workflow.runtime_options = {**workflow.runtime_options, "portable_emulation": True}
    elif template in ("table", "chart", "download"):
        add_workflow_task(workflow, "measure", "batch", 120, 200, {"command": MEASUREMENTS_COMMAND, "inputs": {}})
        source = {"source": "workflow:///measure/outputs/measurements.csv"}
        if template in ("table", "download"):
            add_workflow_task(workflow, "make_table", "table", 400, 120 if template == "download" else 200, {**TASK_SPECS["table"].default_config, "title": "Monthly measurements", "inputs": dict(source)})
        if template == "chart":
            add_workflow_task(workflow, "make_chart", "chart", 400, 200, {**TASK_SPECS["chart"].default_config, "title": "Temperature by month", "chart_type": "line", "x": "month", "y": ["temperature_c"], "inputs": dict(source)})
        if template == "download":
            add_workflow_task(workflow, "download_results", "download", 680, 200, {"archive": "results.zip", "inputs": {"data": "workflow:///measure/outputs/measurements.csv", "table": "workflow:///make_table/outputs/table.csv"}})
    elif template == "upload":
        add_workflow_task(workflow, "upload_data", "upload", 120, 200, {**TASK_SPECS["upload"].default_config, "description": "Upload a CSV with a header row.", "accept": ".csv", "save_as": "data.csv"})
        source = {"source": "workflow:///upload_data/outputs/data.csv"}
        add_workflow_task(workflow, "make_table", "table", 400, 120, {**TASK_SPECS["table"].default_config, "title": "Uploaded data", "inputs": dict(source)})
        add_workflow_task(workflow, "make_chart", "chart", 400, 300, {**TASK_SPECS["chart"].default_config, "title": "Uploaded data", "inputs": dict(source)})
    elif template == "form":
        add_workflow_task(workflow, "fill_form", "form", 120, 200, json.loads(json.dumps(TASK_SPECS["form"].default_config)))
        add_workflow_task(workflow, "use_values", "batch", 420, 200, {"command": ". $values\necho \"Threshold $THRESHOLD for $LABEL\" > summary.txt", "inputs": {"values": "workflow:///fill_form/outputs/form.env"}})
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
    if run.unix_username:
        # Cluster runs live in the user's own directory: ask the runner.
        try:
            with remote_client(run) as client:
                client.delete(run.id)
        except hpc.HpcError as exc:
            current_app.logger.warning("Could not remove cluster files of run %s: %s", run.id, exc)
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


def remote_client(run: WorkflowRun) -> hpc.RunnerClient:
    """Runner client for a cluster run, signing in as the run's own account."""
    user = run.user
    if not user or user.unix_username != run.unix_username:
        raise hpc.HpcError("The cluster account that executed this run is no longer linked.")
    return hpc.client_for(user)


def remote_relative_path(task_run: TaskRun, relative_path: str) -> str:
    """Path of a task file relative to its cluster run directory."""
    run_root = posixpath.normpath(task_run.run.scratch_path)
    task_root = posixpath.normpath(task_run.scratch_path or run_root)
    if task_root != run_root and not task_root.startswith(run_root + "/"):
        raise ValueError("Unsafe scratch path")
    requested = posixpath.normpath(posixpath.join(task_root, (relative_path or "").lstrip("/")))
    if requested != task_root and not requested.startswith(task_root + "/"):
        raise ValueError("Unsafe scratch path")
    return posixpath.relpath(requested, run_root)


def preview_remote_content(name: str, content: bytes) -> dict:
    """The preview payload of ``preview_task_file`` for bytes from the cluster."""
    if b"\0" in content:
        raise ValueError("Binary files cannot be previewed")
    text = content.decode("utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        return {"kind": "geojson" if is_geojson(value) else "json", "content": text}
    suffix = posixpath.splitext(name)[1].lower()
    if suffix in {".csv", ".tsv"}:
        return {"kind": "csv", "content": text, "delimiter": "\t" if suffix == ".tsv" else ","}
    return {"kind": "text", "content": text}


REMOTE_PREVIEW_LIMIT = 1_000_000
REMOTE_DOWNLOAD_LIMIT = 256 * 1024 * 1024


def run_file_path(run: WorkflowRun, relative_path: str) -> Path:
    if run.unix_username:
        raise ValueError("Cluster run files are reached through dagonweb-runner")
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
    if run.unix_username:
        fair = run.remote_meta.get("fair") or {}
        return {"enabled": bool(run.workflow.fair_profile.get("enabled")) or bool(fair), "files": fair.get("files") or [], "report": fair.get("report")}
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
        if isinstance(extension.get("task_type"), str) and extension["task_type"] in INTERACTIVE_TASK_TYPES:
            # DAGonWeb interactive kinds are stored as DAGonStar Batch tasks.
            task_type = extension["task_type"]
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
        if task.get("task_type") == "volume":
            check_volume_provider(name, task.get("config", {}))
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
            if any(item.get("kind") == LINK_EXPLICIT and item["source_name"] == source_uid and item["target_name"] == task["name"] for item in normalized_links):
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
@permission_required("workflows.create")
def create_workflow():
    template = request.args.get("template", "").strip()
    if request.method == "GET" and request.args.get("assistant"):
        wf = Workflow(owner_id=current_user.id, name="New_assistant_workflow", description="Designed with the workflow assistant.", is_public=False)
        db.session.add(wf)
        db.session.commit()
        return redirect(url_for("workflows.editor", workflow_id=wf.id, assistant=1))
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
        wf = Workflow(owner_id=current_user.id, name=validate_dagonstar_name(form.name.data, "Workflow name"), description=form.description.data or "", is_public=bool(form.is_public.data) and current_user.can("workflows.publish"))
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
        wf.is_public = bool(form.is_public.data) and current_user.can("workflows.publish")
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
        wf.is_public = bool(form.is_public.data) and current_user.can("workflows.publish")
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
    from ..agents.llm import is_enabled as assistant_enabled

    return render_template("workflows/editor.html", workflow=wf, editable=can_edit(wf), task_types=TASK_TYPE_LABELS.items(), palette=palette(), assistant_enabled=assistant_enabled())

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
    filename = export_filename(workflow)
    document = workflow.as_json()
    if request.args.get("portable"):
        # Plain DAGonStar document: no editor extension. Interactive tasks stay
        # Batch tasks that read DAGONWEB_DATA / DAGONWEB_PYTHON.
        document.pop("dagonweb", None)
        for entry in document["tasks"].values():
            entry.pop("dagonweb", None)
        filename += ".dagonstar"
    return Response(json.dumps(document, indent=2, sort_keys=True) + "\n", mimetype="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})


def export_filename(workflow: Workflow) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", workflow.name).strip("-") or f"workflow-{workflow.id}"


@bp.get("/<int:workflow_id>/python")
@login_required
def download_workflow_python(workflow_id: int):
    workflow = db.get_or_404(Workflow, workflow_id)
    if not can_access(workflow):
        abort(403)
    source = workflow_python_source(workflow)
    return Response(source, mimetype="text/x-python", headers={"Content-Disposition": f'attachment; filename="{export_filename(workflow)}.py"'})


@bp.get("/<int:workflow_id>/bundle")
@login_required
def download_workflow_bundle(workflow_id: int):
    """Ready-to-use headless application: program, JSON, CWL, dagon.ini, README."""
    workflow = db.get_or_404(Workflow, workflow_id)
    if not can_access(workflow):
        abort(403)
    try:
        cwl = export_cwl(workflow)
    except Exception as exc:  # CWL is optional in the bundle
        current_app.logger.info("CWL export skipped for bundle: %s", exc)
        cwl = None
    data = python_bundle(workflow, cwl)
    return send_file(BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=f"{export_filename(workflow)}.zip")


@bp.get("/<int:workflow_id>/cwl")
@login_required
def download_workflow_cwl(workflow_id: int):
    workflow = db.get_or_404(Workflow, workflow_id)
    if not can_access(workflow):
        abort(403)
    filename = export_filename(workflow)
    try:
        source = export_cwl(workflow)
    except Exception as exc:  # DAGonStar validation errors are user-facing here.
        flash(f"CWL export failed: {exc}", "danger")
        return redirect(url_for("workflows.editor", workflow_id=workflow.id))
    return Response(source, mimetype="application/x-yaml", headers={"Content-Disposition": f'attachment; filename="{filename}.cwl"'})


@bp.post("/upload")
@permission_required("workflows.create")
def upload_workflow():
    """Import a DAGonStar JSON document, a Python program, or a ZIP application."""
    uploaded = request.files.get("workflow_file")
    if not uploaded or not uploaded.filename:
        flash("Choose a workflow file to upload.", "warning")
        return redirect(url_for("workflows.list_workflows"))
    limit = int(settings.get("max_upload_mb") or 50) * 1024 * 1024
    raw = uploaded.read(limit + 1)
    filename = uploaded.filename
    warnings: list[str] = []
    try:
        if len(raw) > limit:
            raise ValueError("The file is larger than the upload limit.")
        dagon_ini: dict | None = None
        if filename.lower().endswith((".py", ".zip")):
            document, warnings = import_python_files(read_upload(filename, raw), fallback_name=re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename).stem) or "imported_workflow")
            dagon_ini = document.pop("dagon_ini", None)
            source = "python"
        else:
            document = json.loads(raw)
            source = "json"
        if not isinstance(document, dict) or not isinstance(document.get("tasks"), dict):
            raise ValueError("Unsupported DAGonStar workflow JSON format.")
        name = document.get("name")
        if not isinstance(name, str):
            raise ValueError("Workflow name is invalid.")
        workflow = Workflow(owner_id=current_user.id, name=validate_dagonstar_name(name, "Workflow name"), description=str(document.get("description", "")), is_public=bool(document.get("is_public", False)) and current_user.can("workflows.publish"))
        if source == "python" and not workflow.description:
            workflow.description = str((document.get("dagonweb") or {}).get("python", {}).get("docstring", ""))[:2000]
        if dagon_ini:
            workflow.dagon_ini = {str(section): {str(key): str(value) for key, value in values.items()} for section, values in dagon_ini.items() if isinstance(values, dict)}
        db.session.add(workflow)
        db.session.flush()
        has_layout = all(isinstance((entry.get("dagonweb") or {}).get("x"), int) for entry in document["tasks"].values())
        apply_workflow_document(workflow, document, auto_layout=source == "json" or not has_layout)
        audit.record("workflow.import", workflow.name, f"{source} from {filename[:120]}")
        db.session.commit()
    except PythonImportError as exc:
        db.session.rollback()
        flash(f"Python import failed: {exc}", "danger")
        return redirect(url_for("workflows.list_workflows"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        db.session.rollback()
        detail = f" ({exc})" if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else ""
        flash(f"The uploaded file is not a valid DAGonWeb workflow document{detail}.", "danger")
        return redirect(url_for("workflows.list_workflows"))
    flash("Workflow imported." + (" " + " ".join(warnings) if warnings else ""), "warning" if warnings else "success")
    return redirect(url_for("workflows.editor", workflow_id=workflow.id))


def start_run_thread(workflow_id: int, run_id: int, resume: bool = False) -> None:
    """Hand a run to the configured execution backend (threads or Redis)."""
    from ..executor.dispatch import submit_run

    submit_run(workflow_id, run_id, resume)


@bp.post("/<int:workflow_id>/run")
@permission_required("workflows.run")
def run_workflow(workflow_id):
    wf = db.get_or_404(Workflow, workflow_id)
    if not can_access(wf):
        abort(403)
    if not wf.tasks:
        flash("Add at least one task before running the workflow.", "warning")
        return redirect(url_for("workflows.editor", workflow_id=wf.id))
    unix_username = None
    if hpc.enabled():
        # A run always executes as the submitter's own cluster account,
        # whoever owns the workflow, administrators included.
        if not current_user.unix_linked:
            flash("Runs on this server execute on the cluster as your own Unix account. Link it first.", "warning")
            return redirect(url_for("account.index", _anchor="clusterTitle"))
        unix_username = current_user.unix_username
    run = WorkflowRun(workflow_id=wf.id, user_id=current_user.id, status="pending", label=(request.form.get("label") or "").strip()[:255], scratch_path="", log="", unix_username=unix_username)
    db.session.add(run)
    db.session.flush()
    audit.record("run.start", wf.name, f"run {run.id}" + (f" as {unix_username}" if unix_username else ""))
    db.session.commit()
    start_run_thread(wf.id, run.id)
    flash("Workflow run started.", "info")
    return redirect(url_for("workflows.run_detail", workflow_id=wf.id, run_id=run.id))


@bp.post("/runs/<int:run_id>/resume")
@permission_required("workflows.run")
def resume_run(run_id: int):
    run = db.get_or_404(WorkflowRun, run_id)
    if not can_manage_run(run):
        abort(403)
    if run.status != "failed":
        flash("Only failed runs can be resumed.", "warning")
        return redirect(url_for("workflows.run_detail", workflow_id=run.workflow_id, run_id=run.id))
    if run.unix_username and not (run.user_id == current_user.id and current_user.unix_linked and current_user.unix_username == run.unix_username):
        flash(f"Only the submitter can resume this run, because it executes as their cluster account ({run.unix_username}).", "warning")
        return redirect(url_for("workflows.run_detail", workflow_id=run.workflow_id, run_id=run.id))
    if not run.unix_username and hpc.enabled():
        flash("This run executed inside DAGonWeb before cluster execution was turned on, so it cannot be resumed. Start a new run.", "warning")
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
    follow_cluster_run(run)
    specs = {key: {"label": spec.label, "icon": spec.icon, "category": spec.category} for key, spec in TASK_SPECS.items()}
    return render_template("workflows/run.html", workflow=wf, run=run, fair=fair_exports(run), specs=specs, can_manage=can_manage_run(run))


def follow_cluster_run(run: WorkflowRun) -> None:
    """Reconnect to an active cluster run that no process follows any more."""
    if run.unix_username and run.status == "running":
        from ..executor.remote import ensure_followed

        if ensure_followed(run):
            db.session.refresh(run)


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
    if run.unix_username:
        fair = run.remote_meta.get("fair") or {}
        if not fair.get("root") or filename not in {item["name"] for item in fair.get("files") or []}:
            abort(404)
        try:
            with remote_client(run) as client:
                content = client.get(run.id, f"{fair['root']}/{filename}", REMOTE_DOWNLOAD_LIMIT)
        except hpc.HpcError as exc:
            return Response(f"Cannot download the file from the cluster: {exc}", status=502, mimetype="text/plain")
        return send_file(BytesIO(content), as_attachment=True, download_name=filename)
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
    follow_cluster_run(run)
    return jsonify({
        "status": run.status,
        "log": run.log or "",
        "created_at": run.created_at.isoformat() + "Z",
        "updated_at": run.updated_at.isoformat() + "Z",
        "can_resume": run.status == "failed" and can_manage_run(run),
        "interactions": pending_interactions(run) if can_manage_run(run) else [],
        "results": run_results(run),
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

# ---------------------------------------------------------------- interactive tasks


def interactive_tasks(run: WorkflowRun, kinds: tuple[str, ...]) -> dict[str, WorkflowTask]:
    return {task.name: task for task in run.workflow.tasks if task.normalized_task_type in kinds}


def interaction_dir(run: WorkflowRun, task_name: str) -> Path:
    from ..executor.local import run_data_path

    return run_data_path(run_file_path(run, ""), task_name)


def pending_interactions(run: WorkflowRun) -> list[dict]:
    """Upload/Form tasks of an active run that are still waiting for the user."""
    if run.status not in ACTIVE_RUN_STATUSES or not run.scratch_path:
        return []
    states = {task_run.task_uid: task_run.status for task_run in run.task_runs}
    pending = []
    provided = set(run.remote_meta.get("inputs") or []) if run.unix_username else set()
    if run.unix_username and run.user_id != current_user.id:
        return []  # inputs of a cluster run are written as its submitter: only they provide them
    for name, task in interactive_tasks(run, ("upload", "form")).items():
        try:
            ready = name in provided if run.unix_username else (interaction_dir(run, name) / ".ready").is_file()
        except ValueError:
            continue
        if ready or states.get(name) not in ("pending", "running"):
            continue
        config = task.config
        item = {"task": name, "kind": task.normalized_task_type, "description": str(config.get("description") or ""), "running": states.get(name) == "running"}
        if item["kind"] == "upload":
            item.update({"accept": str(config.get("accept") or ""), "multiple": bool(config.get("multiple")), "max_mb": min(int(config.get("max_mb") or 50), int(settings.get("max_upload_mb") or 50)), "save_as": safe_filename(config.get("save_as"), "data.csv")})
        else:
            item["fields"] = form_fields(config)
        pending.append(item)
    return pending


def run_results(run: WorkflowRun) -> list[dict]:
    """Tables, charts and downloads produced by finished interactive tasks."""
    if not run.scratch_path:
        return []
    results = []
    task_runs = {task_run.task_uid: task_run for task_run in run.task_runs if task_run.status == "success"}
    if run.unix_username:
        return remote_run_results(run, task_runs)
    for name, task in interactive_tasks(run, ("table", "chart", "download")).items():
        task_run = task_runs.get(name)
        if not task_run:
            continue
        kind = task.normalized_task_type
        try:
            if kind == "download":
                relative = f"outputs/{safe_filename(task.config.get('archive'), 'results.zip')}"
                path = task_run_file_path(task_run, relative)
                if path.is_file():
                    results.append({"task": name, "kind": kind, "name": path.name, "size": path.stat().st_size, "url": url_for("workflows.download_task_run_file", task_run_id=task_run.id, path=relative)})
                continue
            path = task_run_file_path(task_run, f"outputs/{kind}.json")
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        item = {"task": name, "kind": kind, "data": data, "task_run_id": task_run.id}
        if kind == "chart":
            item["svg_url"] = url_for("workflows.download_task_run_file", task_run_id=task_run.id, path="outputs/chart.svg")
        if kind == "table":
            item["csv_url"] = url_for("workflows.download_task_run_file", task_run_id=task_run.id, path="outputs/table.csv")
        results.append(item)
    return results


def remote_run_results(run: WorkflowRun, task_runs: dict) -> list[dict]:
    """Results of a cluster run, as fetched once by the executor (no SSH here)."""
    results = []
    stored = run.remote_meta.get("results") or {}
    for name, task in interactive_tasks(run, ("table", "chart", "download")).items():
        item, task_run = stored.get(name), task_runs.get(name)
        if not item or item.get("missing") or not task_run:
            continue
        kind = task.normalized_task_type
        if kind == "download":
            if item.get("name"):
                results.append({"task": name, "kind": kind, "name": item["name"], "size": item.get("size", 0), "url": url_for("workflows.download_task_run_file", task_run_id=task_run.id, path=item["path"])})
            continue
        entry = {"task": name, "kind": kind, "data": item.get("data"), "task_run_id": task_run.id}
        if kind == "chart":
            entry["svg_url"] = url_for("workflows.download_task_run_file", task_run_id=task_run.id, path="outputs/chart.svg")
        if kind == "table":
            entry["csv_url"] = url_for("workflows.download_task_run_file", task_run_id=task_run.id, path="outputs/table.csv")
        results.append(entry)
    return results


def _accepted(filename: str, accept: str) -> bool:
    rules = [item.strip().lower() for item in accept.split(",") if item.strip()]
    suffixes = [rule for rule in rules if rule.startswith(".")]
    return not suffixes or any(filename.lower().endswith(suffix) for suffix in suffixes)


def interaction_payload(task: WorkflowTask) -> tuple[list[tuple[str, bytes]], str]:
    """The files an Upload/Form task waits for, validated, as (relative path, bytes)."""
    config = task.config
    if task.normalized_task_type == "upload":
        files = [item for item in request.files.getlist("files") if item and item.filename]
        if not files:
            raise ValueError("Choose a file to upload.")
        if len(files) > 1 and not config.get("multiple"):
            raise ValueError("This task accepts a single file.")
        limit = min(int(config.get("max_mb") or 50), int(settings.get("max_upload_mb") or 50)) * 1024 * 1024
        accept = str(config.get("accept") or "")
        payload = []
        for item in files:
            original = secure_filename(item.filename) or "upload.dat"
            if not _accepted(original, accept):
                raise ValueError(f"{original}: accepted types are {accept}.")
            name = original if config.get("multiple") else safe_filename(config.get("save_as"), "data.csv")
            content = item.stream.read(limit + 1)
            if len(content) > limit:
                raise ValueError(f"{original} is larger than {limit // (1024 * 1024)} MB.")
            payload.append((f"files/{name}", content))
        return payload, ", ".join(name.split("/", 1)[1] for name, _ in payload)
    body = request.get_json(silent=True)
    raw = body if isinstance(body, dict) else request.form.to_dict()
    values = coerce_form_values(form_fields(config), raw)
    return [("form.json", form_document(values).encode("utf-8")), ("form.env", form_env(values).encode("utf-8"))], ", ".join(values)


def submit_remote_interaction(run: WorkflowRun, task: WorkflowTask):
    """Write a cluster run's Upload/Form input into its data folder, as the submitter."""
    if run.user_id != current_user.id:
        return jsonify({"error": "Only the person who started this cluster run can provide its inputs."}), 403
    if task.name in (run.remote_meta.get("inputs") or []):
        return jsonify({"error": "This input was already provided."}), 409
    try:
        payload, detail = interaction_payload(task)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    folder = f"_dagonweb/data/{task.name}"
    try:
        with remote_client(run) as client:
            for relative, content in payload:
                client.put(run.id, f"{folder}/{relative}", content)
            client.put(run.id, f"{folder}/.ready", b"")
    except hpc.HpcError as exc:
        return jsonify({"error": f"Cannot send the input to the cluster: {exc}"}), 502
    # The follower writes the same row: lock and re-read it before changing it.
    from ..executor.remote import locked_run

    run = locked_run(run.id)
    run.update_remote_meta(inputs=sorted({*(run.remote_meta.get("inputs") or []), task.name}))
    run.log = (run.log or "") + timestamped_log(f"Input for {task.name} provided by {current_user.name}.")
    audit.record("run.interaction", f"{run.workflow.name}/{task.name}", detail[:500])
    db.session.commit()
    return jsonify({"status": "ok"})


@bp.post("/runs/<int:run_id>/interact/<task_name>")
@permission_required("workflows.run")
def submit_interaction(run_id: int, task_name: str):
    """Receive the files or form values an Upload/Form task waits for."""
    run = db.get_or_404(WorkflowRun, run_id)
    if not can_manage_run(run):
        abort(403)
    task = interactive_tasks(run, ("upload", "form")).get(task_name)
    if not task:
        abort(404)
    if run.status not in ACTIVE_RUN_STATUSES:
        return jsonify({"error": "The run is no longer active."}), 409
    if run.unix_username:
        return submit_remote_interaction(run, task)
    try:
        target = interaction_dir(run, task.name)
    except ValueError:
        abort(403)
    if (target / ".ready").is_file():
        return jsonify({"error": "This input was already provided."}), 409
    config = task.config
    try:
        if task.normalized_task_type == "upload":
            files = [item for item in request.files.getlist("files") if item and item.filename]
            if not files:
                raise ValueError("Choose a file to upload.")
            if len(files) > 1 and not config.get("multiple"):
                raise ValueError("This task accepts a single file.")
            limit = min(int(config.get("max_mb") or 50), int(settings.get("max_upload_mb") or 50)) * 1024 * 1024
            accept = str(config.get("accept") or "")
            folder = target / "files"
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for item in files:
                original = secure_filename(item.filename) or "upload.dat"
                if not _accepted(original, accept):
                    raise ValueError(f"{original}: accepted types are {accept}.")
                name = original if config.get("multiple") else safe_filename(config.get("save_as"), "data.csv")
                destination = safe_child(folder, name)
                written = 0
                with destination.open("wb") as handle:
                    while chunk := item.stream.read(1024 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise ValueError(f"{original} is larger than {limit // (1024 * 1024)} MB.")
                        handle.write(chunk)
                saved.append(name)
            detail = ", ".join(saved)
        else:
            payload = request.get_json(silent=True)
            raw = payload if isinstance(payload, dict) else request.form.to_dict()
            values = coerce_form_values(form_fields(config), raw)
            target.mkdir(parents=True, exist_ok=True)
            (target / "form.json").write_text(form_document(values), encoding="utf-8")
            (target / "form.env").write_text(form_env(values), encoding="utf-8")
            detail = ", ".join(values)
    except ValueError as exc:
        shutil.rmtree(target / "files", ignore_errors=True)
        return jsonify({"error": str(exc)}), 400
    (target / ".ready").touch()
    run.log = (run.log or "") + timestamped_log(f"Input for {task.name} provided by {current_user.name}.")
    audit.record("run.interaction", f"{run.workflow.name}/{task.name}", detail[:500])
    db.session.commit()
    return jsonify({"status": "ok"})


@bp.get("/files")
@login_required
def files():
    path = request.args.get("path", "")
    task_run = TaskRun.query.filter_by(scratch_path=path).first()
    if not task_run or not can_access_run(task_run.run):
        abort(403)
    try:
        if task_run.run.unix_username:
            with remote_client(task_run.run) as client:
                return jsonify(client.ls(task_run.run.id, remote_relative_path(task_run, "")))
        return jsonify(list_files(path))
    except ValueError:
        abort(403)
    except hpc.HpcError as exc:
        return jsonify({"error": str(exc)}), 502


def task_run_file_path(task_run: TaskRun, relative_path: str) -> Path:
    # Stored paths are treated as untrusted too: never let a record point the
    # browser outside the configured scratch root.
    if task_run.run.unix_username:
        raise ValueError("Cluster run files are reached through dagonweb-runner")
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
        if task_run.run.unix_username:
            with remote_client(task_run.run) as client:
                return jsonify({"path": relative_path, "files": client.ls(task_run.run.id, remote_relative_path(task_run, relative_path))})
        base = task_run_file_path(task_run, relative_path)
        return jsonify({"path": relative_path, "files": list_directory(base)})
    except ValueError:
        abort(403)
    except hpc.HpcError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.get("/task-runs/<int:task_run_id>/file")
@login_required
def preview_task_run_file(task_run_id: int):
    task_run = db.get_or_404(TaskRun, task_run_id)
    if not can_access_run(task_run.run):
        abort(403)
    relative_path = request.args.get("path", "")
    try:
        if task_run.run.unix_username:
            with remote_client(task_run.run) as client:
                content = client.get(task_run.run.id, remote_relative_path(task_run, relative_path), REMOTE_PREVIEW_LIMIT)
            return jsonify({"path": relative_path, **preview_remote_content(relative_path, content)})
        return jsonify({"path": relative_path, **preview_task_file(task_run_file_path(task_run, relative_path))})
    except (ValueError, UnicodeDecodeError) as exc:
        return jsonify({"error": str(exc) if isinstance(exc, ValueError) else "The file is not UTF-8 text"}), 400
    except hpc.HpcError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.get("/task-runs/<int:task_run_id>/download")
@login_required
def download_task_run_file(task_run_id: int):
    task_run = db.get_or_404(TaskRun, task_run_id)
    if not can_access_run(task_run.run):
        abort(403)
    try:
        if task_run.run.unix_username:
            relative = remote_relative_path(task_run, request.args.get("path", ""))
            try:
                with remote_client(task_run.run) as client:
                    content = client.get(task_run.run.id, relative, REMOTE_DOWNLOAD_LIMIT)
            except hpc.HpcError as exc:
                return Response(f"Cannot download the file from the cluster: {exc}", status=502, mimetype="text/plain")
            return send_file(BytesIO(content), as_attachment=True, download_name=posixpath.basename(relative) or "download")
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
    if task_run.run.unix_username:
        try:
            relative = remote_relative_path(task_run, "")
            with remote_client(task_run.run) as client:
                content = client.archive(task_run.run.id, relative, REMOTE_DOWNLOAD_LIMIT)
        except ValueError:
            abort(403)
        except hpc.HpcError as exc:
            return Response(f"Cannot archive the folder on the cluster: {exc}", status=502, mimetype="text/plain")
        return send_file(BytesIO(content), mimetype="application/zip", as_attachment=True, download_name=f"{task_run.task_uid}-scratch.zip")
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
