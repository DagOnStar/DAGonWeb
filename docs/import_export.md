# Import and export

## Export

The editor's **Export** menu offers:

| Format | Endpoint | Notes |
| --- | --- | --- |
| **Headless application (ZIP)** | `GET /workflows/<id>/bundle` | `<name>/<name>.py`, the DAGonStar JSON document, the CWL export when available, a sanitised `dagon.ini`, `requirements.txt`, `run.sh` and a README. Unzip and run `./run.sh`. |
| **Python program** | `GET /workflows/<id>/python` | A single, commented DAGonStar program. |
| **DAGonWeb JSON** | `GET /workflows/<id>/download` | The DAGonStar document with the `dagonweb` extension (positions, form values, runtime options). |
| **DAGonStar JSON (portable)** | `GET /workflows/<id>/download?portable=1` | The same document without editor state. |
| **CWL v1.2** | `GET /workflows/<id>/cwl` | Produced by DAGonStar `saveAsCWL`. |

The generated program only needs DAGonStar. It has a header docstring, one commented block per task (`# Reads: …`, `# Runs after: …`), the native Python sources and interactive scripts it needs, and a command-line interface:

```text
--workdir DIR         scratch directory (default ./scratch)
--resume              resume from the checkpoint of a previous run
--input TASK=FILE     provide the file(s) for an Upload task
--form TASK=FILE      provide the JSON answers for a Form task
```

It exits non-zero when a task fails. Secrets (`api_key`, `password`, `token`…) are removed from the bundled `dagon.ini`.

## Import

**Workflows → Import** accepts:

- a DAGonStar or DAGonWeb **JSON** document;
- a DAGonStar **Python program** (`.py`);
- a **ZIP** of a DAGonStar application (for example a bundle exported by DAGonWeb, or your own project with a `requirements.txt`, helper modules and `dagon.ini`).

Python is imported by **static analysis only** (Python's `ast`): the code is never executed. DAGonWeb finds the `Workflow(...)` and `DagonTask(TaskType.X, ...)` calls, resolves literal arguments (strings, numbers, f-strings, simple concatenations, module-level constants and `dict(...)`), and reads `add_dependency_to()` calls as explicit dependencies. Native task callables are taken from the matching module in the ZIP, `requirements.txt` becomes their requirements, and `dagon.ini` becomes the workflow's configuration. Arguments that cannot be resolved statically, and tasks built in loops, are reported as warnings so you can fix them in the editor. ZIP entries are checked for path traversal, size and count.

## Round trip

Import and export are inverse operations: a workflow imported from Python and exported again gives back a working, commented program. Docstrings and the comments above each task are preserved (`dagonweb.python` in the document), and each task carries a small `# dagonweb: {...}` marker with its canvas position, so re-importing an exported program restores the same layout. The test suite round-trips every built-in template through Python and runs the re-generated program.
