# Getting started

1. Start DAGonWeb (`docker compose up --build`, or see [installation](installation.md)) and sign in.
2. On **Workflows**, pick a template from the gallery, for example **Batch diamond**. You can also import a DAGonStar JSON document (such as one from [examples/](../examples)), a DAGonStar Python program, or a ZIP application, or describe the workflow to the ✨ assistant.
3. In the editor, select a task to configure it in the Inspector. Reference producer files with `workflow:///task/path`, or add a named input and use it as `$name` in the command.
4. Drag from a task's right connector to another task's left connector, then choose a data link or an explicit dependency.
5. Changes save automatically. Use **Organize** and **Fit** to tidy the canvas.
6. Optionally, open **Setup** to choose the runtime options (`max_threads`, portable emulation, pre-staging), FAIR provenance, and `dagon.ini` values.
7. Press **Run** and follow the live graph and log.
8. Select a task on the run graph to read its log and browse or preview its scratch files.
9. If a run fails, fix the task and press **Resume**: tasks that already completed are reused from the checkpoint.

See the [user manual](user_manual.md) for complete guidance.
