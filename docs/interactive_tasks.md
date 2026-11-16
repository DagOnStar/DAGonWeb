# Interactive tasks

DAGonWeb adds five task types to the DAGonStar ones, in the **DAGonWeb interactive** palette category. They let a person take part in a run and turn results into tables, charts and downloads on the run page.

| Type | What it does | Outputs |
| --- | --- | --- |
| **Upload** | Pauses the run until a user uploads one or more files on the run page (accepted extensions, maximum count and a timeout are configurable). | The files, under `outputs/`. |
| **Form** | Pauses the run until a user fills a form. Fields are a JSON list of objects with `name`, `label`, `type` (`text`, `textarea`, `number`, `integer`, `boolean`, `select`, `date`, `email`), `default`, `required` and, for `select`, `options`. Values are validated on submission. | `outputs/form.json` and `outputs/form.env` (shell assignments). |
| **Table** | Renders a CSV, TSV or JSON input as a table (optional column selection and row limit). | `table.json`, `table.csv`, `table.html`. |
| **Chart** | Draws a bar, line, scatter or pie chart from a CSV/JSON input. | `chart.json` (rendered with Chart.js on the run page) and `chart.svg`. |
| **Download** | Collects one or more inputs and publishes them as download buttons, optionally as one ZIP. | The files or the archive. |

Downstream tasks consume their outputs with normal data links, for example `workflow:///form/outputs/form.env` or `workflow:///upload/outputs/data.csv`.

## Compatibility

Interactive tasks are stored as standard DAGonStar `batch` tasks: the document's `type` is `batch`, the command is a self-contained POSIX shell + Python standard-library script, and the DAGonWeb kind is kept in the additive `dagonweb.task_type` key. The document therefore still loads with `dagon.Workflow.load_json`, and the exported Python application runs them headless:

```bash
python my_workflow.py --input upload=data.csv --form parameters=answers.json
```

Two placeholders are resolved at run time: `${DAGONWEB_DATA}` (the run's interaction directory, `run_<id>/_dagonweb/data/`, inside the scratch root) and `${DAGONWEB_PYTHON:-python3}`.

On the server, the run page shows a card for each waiting Upload or Form task. Submissions are posted to `/workflows/runs/<run>/interact/<task>` with the CSRF token, file names are sanitised, size limits follow **Admin → Storage → Maximum upload size**, and files are written only inside the run's interaction directory.

The **Upload**, **Form**, **Table**, **Chart** and **Download** templates show each type in action.
