# Architecture

DAGonWeb is organized as a conventional Flask application:

- `app/auth`: login, logout, registration.
- `app/admin`: setup, user CRUD, role CRUD.
- `app/workflows`: workflow CRUD, graph API, visual editor, run pages.
- `app/task_specs.py`: the registry of the 14 DAGonStar task types. It defines the palette metadata, the Inspector fields and defaults, the document fields, and the `DagonTask` arguments for each type.
- `app/executor`: builds and launches the DAGonStar runtime workflow (`build_runtime_workflow`, `execute_workflow`), records events and failures, handles checkpoint resume and CWL export, and inspects scratch directories.
- `app/static`: PWA assets and the JavaScript visual editor.
- `app/templates`: Bootstrap templates.

The database stores workflows as relational rows:

- `Workflow`: metadata and ownership.
- `WorkflowTask`: task name, type, position, and JSON configuration.
- `WorkflowLink`: a directed edge whose `kind` is either `data`, inferred from a DAGonStar `workflow:///` URI, or `explicit`, from `explicit_prevs`.
- `WorkflowRun`: run status and run scratch directory.
- `TaskRun`: per-task status, log, and scratch directory.

The front end is build-free browser JavaScript:

- `editor.js`: the canvas and the Inspector.
- `run.js`: the live run graph and the file browser.
- `theme.js`: the light, dark and automatic themes.
- `service-worker.js`: served at `/service-worker.js`, so its scope covers the whole site.

The editor uses Ace from a CDN for command editing. The README has diagrams of the architecture, the run lifecycle, and the data model.
