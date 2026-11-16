# Execution model

DAGonWeb runs workflows with the DAGonStar runtime (2.9.4, pinned in `requirements.txt`). When you press **Run**, the server takes these steps:

1. **Create a run.** DAGonWeb adds a `WorkflowRun` row (`pending`) and a run directory `run_<id>/` below the configured scratch root, then redirects the browser to the live run page.
2. **Dispatch the run.** With the default `thread` backend, a thread of the web process runs it, at most **Concurrent runs per process** at a time (others wait for a slot). With the `redis` backend, the run is queued on Redis and an RQ worker (`flask --app wsgi worker`) executes it; if Redis is unreachable, DAGonWeb falls back to a local thread. The run loads the workflow's `dagon.ini`:
   - The global `dagon.ini` is merged with the per-workflow settings.
   - `[batch] scratch_dir_base` is always forced to the run directory, and `remove_dir` to `False`. A per-workflow ini therefore cannot write outside the scratch root.
3. **Build the runtime workflow.** `build_runtime_workflow()` creates a `dagon.Workflow` with:
   - `max_threads`, `portable_emulation` and `pre_stage` from the workflow setup.
   - A `checkpoint_file` at `run_<id>/_dagonweb/checkpoint.json`.
4. **Add the tasks.** For each task, `app/task_specs.py` turns the saved configuration into the arguments of `DagonTask(...)`, such as image, partition, specification, or agentic contract.
   - Batch-like tasks get their named inputs prepended as shell assignments (`data=workflow:///extract/data.txt ;`). The script can then use `$data`.
   - Commands are normalised so that a `workflow:///` reference always ends at a space. DAGonStar's reference parser requires this.
5. **Set dependencies.**
   - Explicit edges are added with `add_dependency_to()`.
   - `make_dependencies()` infers the data edges from `workflow:///` references and stages the referenced files into each consumer.
6. **Launch and wait.** The thread calls `launch(resume_checkpoint_file=...)` and then `wait()`. DAGonStar event listeners run on each task's thread and write progress into `TaskRun` rows:
   - `on_task_start`, `on_task_staging_in_start`, `on_task_execute_start`, `on_task_staging_out_start` and `on_task_end`.
   - Each event is timestamped, so the run page can show *staging inputs → executing → staging outputs*.
7. **Report failures.** DAGonStar task threads log their exceptions instead of raising them. DAGonWeb attaches a logging handler, filtered by task thread, that captures the error message and stores it in the failed task's log. Tasks that never start because an upstream task failed are marked as skipped.

Browser navigation never stops a run. With the thread backend, restarting the application process stops runs that are in progress; with the Redis backend, runs live in the workers and survive web restarts (see [deployment.md](deployment.md)).

[Interactive tasks](interactive_tasks.md) (Upload, Form) wait inside the run for input submitted on the run page; Table, Chart and Download tasks publish their results there.

## Resume

Every run writes a DAGonStar checkpoint. On a **failed** run, **Resume** (on the run page or the runs list) restarts it in place:

- It reuses the same run directory and checkpoint.
- Tasks that completed are skipped by DAGonStar.
- Only the failed task and the tasks after it run again.

**Run again** instead always starts a fresh run.

## Portable emulation

When **Portable emulation** is enabled in workflow setup, DAGonStar runs the commands of Docker, Kubernetes, Apptainer, Nomad, Slurm, Cloud and LLM tasks locally, and returns a deterministic response for LLM tasks. Templates for those back-ends start with emulation on, so they run without a cluster, registry or API key. Docker and Kubernetes still need the DAGonStar extras, which are installed by `requirements.txt`.

## Native tasks

Native tasks run a Python callable. If a task lists `requirements`, DAGonWeb creates a virtual environment per run, installs the requirements there, and sets the task's `python` to that environment. Inline `source` is written as a module inside the run directory and imported from there.

## Artifacts

Each task's working directory is a subdirectory of the run directory. On the run page you can select a task and then:

- Browse its directories.
- Preview text, JSON, CSV, GeoJSON and NetCDF files.
- Download single files, or the whole directory as a ZIP archive.

Every path is resolved and checked against the task directory, which itself lies inside the scratch root.

## FAIR provenance

Workflow setup includes an optional FAIR provenance profile. It maps to DAGonStar's native `FairProfile` (`enable_fair`) when the installed DAGonStar package provides `dagon.fair`. When it is enabled, DAGonStar records local metadata under the run directory. The run page then lists these exports:

- `run.json`, `ro-crate-metadata.json`, `prov.json`, `datacite.json` and `codemeta.json`.
- `checksums.sha256` and `fairness-report.json`.
- `report.md` and `report.html`.

The profile is stored in the additive `dagonweb.fair` extension. DAGonWeb never publishes data to remote repositories.

## Exports

- **DAGonStar JSON**: the task-map document. It loads with `dagon.Workflow.load_json`.
- **Python program**: a standalone script that builds the same `DagonTask` objects and explicit edges, plus the runtime options and a checkpoint file.
  - It writes inline native sources next to itself.
  - It supports `--resume`.
  - It exits non-zero if any task fails.
- **CWL v1.2**: produced by DAGonStar `saveAsCWL` in a temporary directory under the scratch root.
