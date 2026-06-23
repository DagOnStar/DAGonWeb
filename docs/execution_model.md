# Execution model

The demo executor runs locally. It creates a run directory:

```text
<SCRATCH_DIR>/workflow-<id>/run-<timestamp>/
```

Each task receives a subdirectory:

```text
<SCRATCH_DIR>/workflow-<id>/run-<timestamp>/<task_uid>/
```

Supported demo task behavior:

- `input`: writes configured content to a file.
- `bash`: executes a shell command in the task scratch directory.
- `python`: writes and runs a Python script in the task scratch directory.
- `native`: writes DAGonStar-style native task metadata for adapter integration.
- `llm`: writes prompt metadata for later OpenAI-compatible integration.

Before running, the executor checks that the graph is a directed acyclic graph. DAGonStar-specific execution can be added behind the same interface by translating `WorkflowTask` and `WorkflowLink` rows into DAGonStar task declarations.
