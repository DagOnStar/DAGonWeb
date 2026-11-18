# AGENTS.md

## Project
DAGonWeb is a Flask/Bootstrap Progressive Web Application for creating, editing, importing, exporting, and executing DAGonStar workflows with a visual editor.

## Goals
- Keep the project simple, readable, and deployable with Docker Compose.
- Use DAGonStar as the execution runtime. The supported version is pinned in `requirements.txt`, currently 2.9.4 with the `docker` and `kubernetes` extras.
- Build data dependencies from `workflow:///<task>/<path>` references and explicit dependencies from `explicit_prevs`. Never use imported `prevs` or `nexts` fields, except as the DAGonStar fallback: a document without `explicit_prevs` treats its legacy `prevs` as explicit edges. `WorkflowLink.kind` is either `data` or `explicit`.
- `app/task_specs.py` is the single registry of task types. It defines the palette and Inspector fields, the default configs, the DAGonStar document fields, and the `DagonTask` arguments. To add a DAGonStar option, change the registry rather than the templates or the JavaScript.
- DAGonWeb interactive task types (Upload, Form, Table, Chart, Download) are registered in `app/task_specs.py` with `dagon_type="batch"`; their scripts live in `app/interactive.py`. In the document their `type` is `batch` and the DAGonWeb kind is `dagonweb.task_type`.
- Python export and import live in `app/pycode.py`. Import uses `ast` only and must never execute uploaded code. Exporting an imported workflow must give back working, commented Python (round-trip tests in `tests/test_platform.py`).
- Administration settings are declared in `app/settings.py` (one `SettingDef` per key, overridable with `DAGONWEB_<KEY>`). Permissions are declared in `PERMISSIONS` in `app/models.py`; protect routes with `app/authz.py`.
- Runs are dispatched by `app/executor/dispatch.py` (thread pool or Redis/RQ). The LLM assistant (`app/agents/`) edits drafts only; the user applies them.
- Preserve the DAGonStar task-map JSON document (`tasks` keyed by task name). DAGonWeb metadata may live only in the additive `dagonweb` extension.
- Use SQLAlchemy models and migrations for persistent state.
- Use role-based access: `admin` can manage users, roles, workflows, and settings; `user` can manage own workflows and runs.
- Treat scratch directories as potentially sensitive. Never expose arbitrary filesystem paths outside the configured scratch root.
  - Task directories, the checkpoint (`run_<id>/_dagonweb/checkpoint.json`), native virtual environments, and CWL exports all live inside the run directory or the scratch root.
  - `workflow_config()` must keep forcing `[batch] scratch_dir_base` and `remove_dir=False` after merging the per-workflow ini.
- Runtime options (`portable_emulation`, `pre_stage`, `max_threads`) live in `dagonweb.runtime`. Templates for remote back-ends start with portable emulation on, so every template runs locally.
- Keep all documentation updated.
- Keep all examples updated. `examples/*.json` are generated from the built-in templates with `flask --app wsgi export-examples`. A test fails when they are stale.
- Update the main README.md.
- Enforce the automatic testing to validate modifications. `tests/test_dagonstar_alignment.py` checks several things:
  - The registry matches DAGonStar's `TaskType`.
  - Every default config builds a `DagonTask`.
  - Every template runs end-to-end.
  - Resume works, and failure messages are captured.
  - The generated Python program runs.
  - The examples are up to date.
- Add an Alembic migration in `migrations/versions/` for every model change.
- Check UI changes in a browser: light and dark themes, and phone width with no horizontal scroll.
- Accessibility (WCAG 2.1 AA): render forms with the `_forms.html` macros, give every control a label, keep text contrast at 4.5:1 in both themes, keep keyboard alternatives for pointer interactions, and keep `tests/test_compliance.py` passing.
- GDPR: new cookies or browser storage must be listed in `STORAGE_ITEMS` in `app/privacy.py` (and preferences written only when `DAGonWebConsent.allows('preferences')`). New personal data must be included in `export_user_data()` and removed or anonymised by `erase_user()`.
- The guest playground (`app/playground.py`) must never execute workflows, import Native code, or store anything for anonymous visitors.

## Commands
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
flask --app wsgi db upgrade
flask --app wsgi seed
flask --app wsgi run
```

Docker:
```bash
docker compose up --build
```

Tests:
```bash
pytest
```

Examples:
```bash
flask --app wsgi export-examples
```

## Style
- Python: type hints where practical, small functions, no hidden global mutable state.
- Templates: Bootstrap 5, accessible labels, progressive enhancement.
- JavaScript: plain browser JavaScript, no build step required.
- Security: use Flask-WTF CSRF for normal forms; keep API mutations behind login and authorization.

## Review checklist
- Does the change preserve ownership/admin permissions?
- Does the executor use DAGonStar `launch()` / `wait()` (with `resume_checkpoint_file` for resume) and keep files inside the configured scratch directory?
- Do DAGonStar event listeners stay thread-safe? They must capture ids, take the lock, and open an app context for each event.
- Does the editor derive links from `workflow:///` references and `explicit_prevs`, and save the DAGonStar document format, so that the result still loads with `dagon.Workflow.load_json`?
- Are file names and other user-provided strings escaped before they are inserted into the DOM?
- Does the PWA still load `manifest.webmanifest` and register the service worker from `/service-worker.js` with scope `/`?
- Are README.md, docs/, the screenshots in `docs/images/`, and the examples updated?
