# Workflow API

All workflow endpoints require an authenticated user and enforce ownership or administrator access.

- `GET /workflows/<id>/graph` returns the DAGonStar workflow document.
- `POST /workflows/<id>/graph` saves that document.
- `GET /workflows/<id>/download` downloads JSON.
- `POST /workflows/upload` imports DAGonStar JSON.
- `GET /workflows/<id>/python` downloads generated DAGonStar Python source.
- `GET /workflows/<id>/runs/<run>/status` returns the persisted live run/task statuses.

The graph document uses `tasks` keyed by task name. Each task has DAGonStar fields such as `type`, `command`, `nexts`, and `prevs`; editor position and configuration are stored under the additive `dagonweb` object. Imports ignore `nexts`/`prevs` and infer links exclusively from `workflow:///producer/path` references.
