# Workflow editor

The editor has three panes:

- A **task palette**, grouped into Shell & HPC, Containers & orchestrators, Cloud/web/devices, and Python & AI, with a filter box.
- A zoomable **canvas**.
- A resizable **Inspector**. Use **Expand** to let it fill the canvas area, and **Standard** to restore it.

All 14 DAGonStar task types are available. The palette and the Inspector forms are generated from the registry in `app/task_specs.py`, so a new DAGonStar option is added in one place.

## Tasks

- **Add a task**: click a palette item or drag it onto the canvas.
- **Name it**: task names are unique and are the identity used by DAGonStar references. They use letters, numbers, `_` and `-`.
- **Rename it**: renaming rewrites the matching `workflow:///old/...` references and the `explicit_prevs` entries in every task.
- **Configure it**: the Inspector shows the basic fields first. An *Advanced* section holds the rest, and a raw JSON view is available for anything else.
  - Every multi-line field uses the [Monaco editor](text_editing.md): Bash and Python scripts, JSON (validated against a schema from the task registry), `name = value` mappings, lists and `requirements.txt`.
  - Type `workflow:///` (or press Ctrl/⌘+Space) to complete task names and then the files that task produces; unknown tasks are underlined as warnings.
  - While a field has focus, the *Insert a producer reference* buttons insert `workflow:///task/output` at the cursor.
- **Named inputs**: one `name = value` per line. Each name becomes a shell variable, for example `$data`, that holds the staged path. A value can be a `workflow:///task/path` reference or a plain value.
- **Duplicate or delete** a task with the Inspector buttons or the keyboard shortcuts.

## Links

To connect two tasks, drag from a producer's right connector to a consumer's left connector. A dialog lets you choose the kind of link:

- **Data link**: choose a producer file, with suggestions taken from the producer's outputs, and an input name. DAGonWeb adds `name = workflow:///producer/file` to the consumer's inputs. The link is drawn as a solid line.
- **Explicit dependency**: the consumer runs after the producer without staging any data. The producer is added to the consumer's `explicit_prevs`, and the link is drawn dashed.

To remove a link, click it. Links are always derived from the saved configuration:

- `workflow:///task/path` and `workflow://<this workflow>/task/path` draw data links.
- `explicit_prevs` draws explicit links.
- References to other workflows stay in the configuration but draw nothing.

## Export menu

**Export** downloads the workflow as a headless application ZIP, a Python program, DAGonWeb JSON, portable DAGonStar JSON, or CWL. See [Import and export](import_export.md).

## Assistant

**✨ Assistant** opens the [workflow assistant](assistant.md). Its draft replaces the canvas only when you press **Apply to canvas**; the change is saved like any other edit.

## Saving

Every change is saved automatically after a short pause, and the header shows *Saving…*, *All changes saved*, or the error. Leaving the page with unsaved changes asks for confirmation. **Run** saves first and then starts the run. **Organize** arranges the tasks in dependency layers, and **Fit** zooms to show the whole workflow.

## Keyboard shortcuts

| Keys | Action |
| --- | --- |
| Ctrl/⌘ + S | Apply the Inspector and save |
| Ctrl/⌘ + Enter | Apply the Inspector |
| Ctrl/⌘ + D | Duplicate the selected task |
| Delete | Delete the selected task |
| Arrow keys (Shift for faster) | Move the selected task |
| Esc | Clear the selection |

Press **?** in the editor toolbar to see these shortcuts.
