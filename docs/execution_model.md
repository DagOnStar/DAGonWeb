# Execution model

DAGonWeb constructs a DAGonStar `Workflow` from the persisted task-map document. It launches the runtime asynchronously with `launch()` and waits in a server-side worker; browser navigation does not stop the run.

Before execution, DAGonStar discovers dependencies from `workflow:///task/path` references and stages referenced task outputs. Run and task statuses are persisted in the database and shown on the live run graph.

Run artifacts are located below the configured scratch root. Completed task nodes expose a restricted file browser, UTF-8 preview, individual file download, and scratch-directory ZIP download. Restarting the application process itself still requires a durable worker system for recovery.

Workflow setup includes an optional FAIR provenance profile that maps to DAGonStar's native `FairProfile` support when the installed DAGonStar package provides `dagon.fair`. When enabled, runs ask DAGonStar to record local metadata under the run scratch directory and the run page exposes the expected FAIR export files: `run.json`, `ro-crate-metadata.json`, `prov.json`, `datacite.json`, `codemeta.json`, `checksums.sha256`, `fairness-report.json`, `report.md`, and `report.html`.

The FAIR UI stores profile metadata in the additive workflow-level `dagonweb.fair` extension, so exported DAGonStar task-map documents remain portable. DAGonWeb does not publish data to remote repositories and never exposes arbitrary filesystem paths; FAIR exports are downloadable only after resolving them inside the configured scratch root.
