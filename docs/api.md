# Workflow API

All workflow endpoints need a signed-in user. They allow a request only from the workflow's owner or an administrator. Public workflows can also be read and run by other signed-in users. JSON mutations need the `X-CSRFToken` header; form posts need the `csrf_token` field.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/workflows/<id>/graph` | Return the DAGonStar task-map document. |
| `POST` | `/workflows/<id>/graph` | Validate and save a document. Links are rebuilt from `workflow:///` references and `explicit_prevs`. |
| `GET` | `/workflows/<id>/download` | Download the document as JSON. Add `?portable=1` to drop the `dagonweb` editor state. |
| `GET` | `/workflows/<id>/bundle` | Download the headless application ZIP (program, JSON, CWL, `dagon.ini`, `requirements.txt`, `run.sh`, README). |
| `POST` | `/workflows/upload` | Import a JSON document, a DAGonStar Python program (`.py`) or a ZIP application as a new workflow. Python is parsed, never executed. |
| `GET` | `/workflows/<id>/python` | Download a standalone DAGonStar Python program. It supports `--resume`. |
| `GET` | `/workflows/<id>/cwl` | Download a CWL v1.2 export produced by DAGonStar `saveAsCWL`. |
| `POST` | `/workflows/<id>/run` | Start an asynchronous run. Empty workflows are rejected. |
| `POST` | `/workflows/runs/<run>/resume` | Resume a failed run from its DAGonStar checkpoint. |
| `GET` | `/workflows/<id>/runs/<run>/status` | Return the run and task statuses, logs, timestamps, `can_resume`, the pending `interactions` (Upload and Form tasks waiting for input) and the `results` (tables, charts, downloads). |
| `POST` | `/workflows/runs/<run>/interact/<task>` | Answer a waiting Upload (multipart `files`) or Form (JSON `values`) task. |
| `GET` | `/workflows/task-runs/<task_run>/files?path=` | List a directory inside the task's scratch directory. |
| `GET` | `/workflows/task-runs/<task_run>/file?path=` | Return a structured preview of a file (text, JSON, CSV, GeoJSON, NetCDF). |
| `GET` | `/workflows/task-runs/<task_run>/download?path=` | Download one file. |
| `GET` | `/workflows/task-runs/<task_run>/archive` | Download the task's scratch directory as a ZIP archive. |

## Assistant API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/assistant/status` | Whether the LLM assistant is enabled for the current user. |
| `POST` | `/assistant/workflows/<id>/chat` | Body `{messages, draft}`. Runs the agent and returns `{reply, trace, changed, draft, check}`. `503` when the connector is disabled, `403` without `assistant.use`. |
| `POST` | `/assistant/workflows/<id>/validate` | Body `{draft}`. Static checks and DAGonStar dry run of a document, without an LLM. |

## Permissions

Creating and importing workflows needs `workflows.create`, starting or resuming runs needs `workflows.run`, making a workflow public needs `workflows.publish`. See [administration.md](administration.md).

## Document format

The document uses `tasks` keyed by task name. Each task has the fields that DAGonStar 2.9 writes in `Workflow.as_json()`:

- Common fields: `name`, `type`, `command`, `prevs`, `nexts`, `explicit_prevs`, `status`, `working_dir`.
- The fields of its own task type, for example `image` for Docker, `partition` and `time` for Slurm, `callable` for Native, or `specification` for Web and FaaS.

A top-level `pre_stage` is written when scheduler-aware pre-staging is enabled.

DAGonWeb-only state lives in additive `dagonweb` objects:

- Per task: `dagonweb.x`, `dagonweb.y`, `dagonweb.config` and, for [interactive tasks](interactive_tasks.md), `dagonweb.task_type` (the document `type` is then `batch`).
- At workflow level: `dagonweb.runtime` (`portable_emulation`, `pre_stage`, `max_threads`), `dagonweb.fair` and, for workflows imported from Python, `dagonweb.python` (source file, docstring and per-task comments).

On import, `prevs` and `nexts` are ignored. Local links are inferred in two ways:

- **Data links** come from `workflow:///producer/path` and `workflow://<current workflow>/producer/path` references.
- **Explicit dependencies** come from `explicit_prevs`. A plain DAGonStar document without `explicit_prevs` treats its legacy `prevs` as explicit edges, the same way DAGonStar does.

References to other workflow names stay in the task configuration but do not create local links. Unknown tasks, self references, and cycles are rejected; a cycle error names the tasks in the cycle.

The files in [examples/](../examples) are valid documents for every task type.
