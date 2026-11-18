# DAGonWeb user manual

## Without an account

Visitors who are not signed in can open **Try it**, pick a template, edit it, and press **Dry run** to see how DAGonStar would execute it. Nothing runs and nothing is stored on the server; see [Guest playground](playground.md).

## Workflows

Create a workflow from the Workflows page in one of three ways:

- Pick a template from the gallery. There is one runnable template per task type.
- Drop or choose a file in the import area: a DAGonStar JSON document, a DAGonStar Python program (`.py`), or a ZIP of a DAGonStar application. See [Import and export](import_export.md). The [examples/](../examples) folder has a JSON document for every task type.
- Use **New workflow** to start from an empty canvas, or **✨ Design with assistant** to describe the workflow to the [LLM assistant](assistant.md) (when an administrator has enabled it).

Each workflow card shows the task types it uses and the status of its last run. From the card you can open the editor, run the workflow, or open **Setup**. The **More** menu has **Edit metadata**, **Rename**, the exports (JSON, Python, headless application ZIP, CWL), and **Delete**. Use the filter box to find workflows by name, owner or task type. The toolbar also provides **Delete all** for workflows the signed-in user is allowed to manage.

Public workflows can be opened and run by other signed-in users, but only the owner or an administrator can edit metadata or delete them. Administrators can manage every workflow.

Workflow names, task names, input names, and file path segments used in `workflow://workflow/task/path` references must use DAGonStar-safe names: letters, numbers, underscores, hyphens, and dots for file extensions. `workflow:///task/path` is the shorthand for the current workflow, so in workflow `abcd`, `workflow:///task/path` and `workflow://abcd/task/path` refer to the same producer. Spaces, absolute paths, and `..` segments are rejected.

The supported import shape has a `name` and a `tasks` object keyed by task name. DAGonWeb retains editor coordinates and task configuration in a `dagonweb` extension. On import, `nexts` and `prevs` are ignored. Data links are inferred from `workflow:///` and `workflow://current_workflow/...` references, and explicit dependencies are read from `explicit_prevs`. A plain DAGonStar document without `explicit_prevs` treats its legacy `prevs` as explicit edges. A document without a `dagonweb` extension is imported too: DAGonWeb builds the editor configuration from the DAGonStar fields of each task.

## Editing a workflow

Select a task type in the palette to add it. Task names must contain letters, numbers, underscores, or hyphens and must be unique. When a task is renamed, DAGonWeb updates matching `workflow:///old_task/...` and `workflow://current_workflow/old_task/...` references in the workflow configuration. Use the Inspector to configure a task. The Inspector can be resized, or expanded into the canvas area for larger forms and restored to the standard width. Changes made through Apply, dragging tasks, connecting handles, or organizing the canvas save automatically.

To connect tasks, drag the right connector of a producer to the left connector of a consumer, then choose the kind of link:

- **Data link**: DAGonWeb adds a named input `name = workflow:///producer/file` to the consumer and draws a solid link.
- **Explicit dependency**: the consumer runs after the producer without staging data. The producer is added to `explicit_prevs`, and the link is drawn dashed.

Click a link to remove it. See the [workflow editor](workflow_editor.md) for the keyboard shortcuts.

### Interactive tasks

The **DAGonWeb interactive** palette category adds Upload, Form, Table, Chart and Download tasks. Upload and Form pause the run until someone provides files or answers on the run page; Table, Chart and Download show results there. See [Interactive tasks](interactive_tasks.md).

### Assistant

**✨ Assistant** in the editor toolbar opens a chat panel. Ask it to design, change, explain or fix the workflow; it edits a draft, dry-runs it, and shows the result. **Apply to canvas** saves the draft; **Check workflow** runs the dry run on the current canvas. See [Workflow assistant](assistant.md).

### Batch scripts

Batch tasks use the Ace Bash editor. Named inputs become shell variables, so an input `data = workflow:///extract/data.txt` can be used as `$data` in the script. While the editor has focus, clicking a canvas task inserts `workflow:///task/output` at the cursor. Replace `output` with the actual producer-relative output path when needed, for example:

```bash
cat workflow:///prepare/output/data.csv > result.csv
```

## Running workflows

Click **Run** to open the run page immediately. DAGonStar executes the workflow asynchronously on the server. Leaving the page or closing the browser does not stop the run. While it runs:

- The graph, the progress bar, and the live log update.
- Each task shows its current stage: staging inputs, executing, or staging outputs.
- The log of a failed task includes the captured error.
- Upload and Form tasks show a card where you provide their input; tables, charts and download buttons appear when the tasks that produce them finish.

If a run fails, fix the task and press **Resume**. DAGonStar reuses the outputs of the tasks that completed, using the run's checkpoint, and runs only the rest. **Run again** always starts a fresh run.

### Workflow setup

The **Setup** page holds:

- The name, description, and public flag.
- The common `dagon.ini` values: batch threads, DAGon service, and FTP host.
- The DAGonStar runtime options:
  - `max_threads`: the number of tasks that can run at once.
  - **Portable emulation**: run container, scheduler, cloud and LLM tasks locally.
  - **Pre-staging**: stage inputs before scheduling.
- The FAIR provenance profile.
- The raw per-workflow `dagon.ini`. Sections you add there, such as agent profiles, are preserved.

When a task finishes, click its graph node to inspect its scratch directory. You can browse subdirectories, preview text, JSON, CSV, GeoJSON and NetCDF files, download individual files, or download all task artifacts as a ZIP archive. The browser cannot navigate outside that task’s scratch directory.

## Your account and your data

Click your name in the navigation bar to open **Your account**: change your display name and password, **download your data** (JSON), or **delete your account** with all your workflows, runs and files. **Cookie settings** in the page footer changes your cookie choice at any time. See the privacy notice (`/privacy`) for details.

## Permissions and safety

Users manage their own workflows and runs. Administrators can manage all workflows, users, roles, and the site settings (see [Administration](administration.md)). What each user may do (create, run, publish, use the assistant) depends on the permissions of their roles. A Batch command is executable code: only run workflows from trusted users and deploy untrusted workloads in isolated infrastructure.
