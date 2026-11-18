# Architecture

DAGonWeb is organized as a conventional Flask application:

- `app/auth`: login, logout, registration, and single sign-on (`sso.py`: Google, Microsoft Entra ID, generic OpenID Connect).
- `app/admin`: the administration panel: dashboard, settings sections, users, roles, audit log, `dagon.ini`.
- `app/settings.py`: the registry of administration settings (sections, types, defaults, `DAGONWEB_<KEY>` overrides, secrets).
- `app/authz.py`: the `permission_required` / `admin_required` decorators; `app/audit.py`: the audit log; `app/branding.py`: logo and branding; `app/mail.py`: SMTP; `app/maintenance.py`: run retention.
- `app/agents`: the LLM workflow assistant: an OpenAI-compatible client (`llm.py`), the draft-editing and dry-run tools (`tools.py`), and the agent loop (`assistant.py`).
- `app/interactive.py`: the scripts and helpers behind the Upload, Form, Table, Chart and Download task types.
- `app/playground.py`: the guest playground and the `/try/dry-run` endpoint.
- `app/privacy.py`, `app/legal.py`, `app/account.py`: GDPR support (cookie inventory, data export, erasure, retention), the privacy notice, cookie policy and accessibility statement, and the self-service account page.
- `app/pycode.py`: the Python exporter (commented program, headless ZIP bundle) and the AST-only Python/ZIP importer.
- `app/workflows`: workflow CRUD, graph API, visual editor, run pages.
- `app/task_specs.py`: the registry of the 14 DAGonStar task types and the 5 DAGonWeb interactive types. It defines the palette metadata, the Inspector fields and defaults, the document fields, and the `DagonTask` arguments for each type.
- `app/executor`: `dispatch.py` sends runs to a bounded thread pool or a Redis (RQ) queue (`app/jobs.py` is the worker entry point); `local.py` builds and launches the DAGonStar runtime workflow (`build_runtime_workflow`, `execute_workflow`), records events and failures, handles checkpoint resume and CWL export, and inspects scratch directories.
- `app/static`: PWA assets and the JavaScript visual editor.
- `app/templates`: Bootstrap templates.

The database stores workflows as relational rows:

- `Workflow`: metadata and ownership.
- `WorkflowTask`: task name, type, position, and JSON configuration.
- `WorkflowLink`: a directed edge whose `kind` is either `data`, inferred from a DAGonStar `workflow:///` URI, or `explicit`, from `explicit_prevs`.
- `WorkflowRun`: run status and run scratch directory.
- `TaskRun`: per-task status, log, and scratch directory.
- `User`, `Role` (with a JSON permission list), `Setting` and `AuditLog`.

The front end is build-free browser JavaScript:

- `code-editor.js`: upgrades every multi-line text field (`textarea[data-code-language]`, read-only `pre[data-code-language]`) to the Monaco editor and adds the DAGonWeb languages, completion, hover and diagnostics (`window.DAGonWebCode`). See [Text editing](text_editing.md).
- `editor.js`: the canvas and the Inspector.
- `run.js`: the live run graph and the file browser.
- `theme.js`: the light, dark and automatic themes (default from the Appearance settings).
- `assistant.js`: the assistant chat panel.
- `consent.js`: the cookie banner and consent API (`window.DAGonWebConsent`); preference storage is written only with consent.
- `service-worker.js`: served at `/service-worker.js`, so its scope covers the whole site.

Monaco 0.52.2 is loaded on demand from cdnjs; if it cannot be loaded the plain textareas keep working. The README has diagrams of the architecture, the run lifecycle, and the data model.
