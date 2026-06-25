# DAGonWeb user manual

## Workflows

Create a workflow from the Workflows page, start from one of the built-in templates, or import a DAGonStar JSON document. Each workflow card lets authorized users open the editor, rename the workflow, edit metadata, run the workflow, export JSON, generate readable DAGonStar Python source, or delete the workflow and its run history. The toolbar also provides **Delete all** for workflows the signed-in user is allowed to manage.

Public workflows can be opened and run by other signed-in users, but only the owner or an administrator can edit metadata or delete them. Administrators can manage every workflow.

Workflow names, task names, input names, and file path segments used in `workflow://workflow/task/path` references must use DAGonStar-safe names: letters, numbers, underscores, hyphens, and dots for file extensions. `workflow:///task/path` is the shorthand for the current workflow, so in workflow `abcd`, `workflow:///task/path` and `workflow://abcd/task/path` refer to the same producer. Spaces, absolute paths, and `..` segments are rejected.

The supported import shape has a `name` and a `tasks` object keyed by task name. DAGonWeb retains editor coordinates and task configuration in a `dagonweb` extension. On import, `nexts` and `prevs` are ignored; local links are inferred from `workflow:///` references and `workflow://current_workflow/...` references.

## Editing a workflow

Select a task type in the palette to add it. Task names must contain letters, numbers, underscores, or hyphens and must be unique. When a task is renamed, DAGonWeb updates matching `workflow:///old_task/...` and `workflow://current_workflow/old_task/...` references in the workflow configuration. Use the Inspector to configure a task. The Inspector can be resized, or expanded into the canvas area for larger forms and restored to the standard width. Changes made through Apply, dragging tasks, connecting handles, or organizing the canvas save automatically.

To connect tasks, drag the right connector of a producer to the left connector of a consumer. DAGonWeb writes a `workflow:///producer/output` value into the consumer configuration and draws the link.

### Batch scripts

Batch tasks use the Ace Bash editor. Enter creates a new statement and adds `;` when required. **Beautify** places statements on separate lines. While the editor has focus, clicking a canvas task inserts `workflow:///task/output` at the cursor. Replace `output` with the actual producer-relative output path when needed, for example:

```bash
cat workflow:///prepare/output/data.csv > result.csv
```

## Running workflows

Click **Run** to open the run page immediately. DAGonStar executes the workflow asynchronously on the server. The run graph updates task status while it runs; leaving the page or closing the browser does not stop it.

When a task finishes, click its graph node to inspect its scratch directory. You can browse subdirectories, preview UTF-8 text files, download individual files, or download all task artifacts as a ZIP archive. The browser cannot navigate outside that task’s scratch directory.

## Permissions and safety

Users manage their own workflows and runs. Administrators can manage all workflows, users, roles, and the scratch setting. A Batch command is executable code: only run workflows from trusted users and deploy untrusted workloads in isolated infrastructure.
