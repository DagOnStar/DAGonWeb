# Internal graph API

The editor uses two authenticated JSON endpoints:

```http
GET /workflows/<id>/graph
POST /workflows/<id>/graph
```

The POST payload shape is:

```json
{
  "tasks": [
    {"uid": "task_a", "label": "Task A", "task_type": "bash", "x": 100, "y": 100, "config": {}}
  ],
  "links": [
    {"source_uid": "task_a", "source_output": "output", "target_uid": "task_b", "target_input": "input"}
  ]
}
```

The server generates the canonical `workflow_uri` for each link.
