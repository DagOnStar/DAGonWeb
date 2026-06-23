# Architecture

DAGonWeb is organized as a conventional Flask application:

- `app/auth`: login, logout, registration.
- `app/admin`: setup, user CRUD, role CRUD.
- `app/workflows`: workflow CRUD, graph API, visual editor, run pages.
- `app/executor`: local execution and scratch directory inspection.
- `app/static`: PWA assets and the JavaScript visual editor.
- `app/templates`: Bootstrap templates.

The database stores workflows as relational rows:

- `Workflow`: metadata and ownership.
- `WorkflowTask`: task UID, task type, position, and JSON configuration.
- `WorkflowLink`: directed edge plus generated `workflow://` URI.
- `WorkflowRun`: run status and run scratch directory.
- `TaskRun`: per-task status, log, and scratch directory.

The visual editor is intentionally build-free browser JavaScript to keep the project easy to deploy and inspect.
