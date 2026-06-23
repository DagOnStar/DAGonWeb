# Getting started

DAGonWeb provides a browser workflow builder for DAGonStar-style DAGs.

1. Start the app with Docker Compose.
2. Register a new user or sign in as the seeded admin.
3. Create a workflow.
4. Open the editor.
5. Add tasks from the palette.
6. Click a source task, then a target task, to create a directed link.
7. Select each task to configure its JSON settings.
8. Save the graph.
9. Run the workflow.
10. Inspect the run page to see per-task logs and files.

A link from `task_a` to `task_b` persists `workflow://task_a/output`. This is available in the target task environment as `INPUT_<TARGET_INPUT>` during execution.
