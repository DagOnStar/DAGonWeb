# Workflow editor

The editor uses a three-panel workflow layout:

- Left: task palette.
- Center: workflow canvas.
- Right: inspector.

Task placement is click-based. A new task is created with default configuration for the selected task type.

Connections are created by selecting the source task and then selecting the target task. The saved link includes:

```json
{
  "source_uid": "task_a",
  "source_output": "output",
  "target_uid": "task_b",
  "target_input": "input",
  "workflow_uri": "workflow://task_a/output"
}
```

Task configuration is stored as JSON, allowing task-type-specific settings without schema migrations.
