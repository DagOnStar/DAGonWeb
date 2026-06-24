# Execution model

DAGonWeb constructs a DAGonStar `Workflow` from the persisted task-map document. It launches the runtime asynchronously with `launch()` and waits in a server-side worker; browser navigation does not stop the run.

Before execution, DAGonStar discovers dependencies from `workflow:///task/path` references and stages referenced task outputs. Run and task statuses are persisted in the database and shown on the live run graph.

Run artifacts are located below the configured scratch root. Completed task nodes expose a restricted file browser, UTF-8 preview, individual file download, and scratch-directory ZIP download. Restarting the application process itself still requires a durable worker system for recovery.
