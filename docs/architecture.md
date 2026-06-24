# Architecture

DAGonWeb is organized as a conventional Flask application:

- `app/auth`: login, logout, registration.
- `app/admin`: setup, user CRUD, role CRUD.
- `app/workflows`: workflow CRUD, graph API, visual editor, run pages.
- `app/executor`: DAGonStar runtime launch, lifecycle persistence, and scratch directory inspection.
- `app/static`: PWA assets and the JavaScript visual editor.
- `app/templates`: Bootstrap templates.

The database stores workflows as relational rows:

- `Workflow`: metadata and ownership.
- `WorkflowTask`: task name, type, position, and JSON configuration.
- `WorkflowLink`: directed edge inferred from a DAGonStar `workflow:///` URI.
- `WorkflowRun`: run status and run scratch directory.
- `TaskRun`: per-task status, log, and scratch directory.

The visual editor is build-free browser JavaScript. It uses Ace from a CDN for Batch/Bash editing.
