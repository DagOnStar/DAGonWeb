# Guest playground (dry-run mode)

By default, visitors who are not signed in land on **Try it** (`/try/`). They can open any template or an empty canvas in the normal editor, without an account.

- The workflow lives in the browser tab (`sessionStorage`) and is **never stored on the server**. **Download JSON** saves it, **Open JSON** loads a document, **Reset** reloads the template.
- **▶ Dry run** sends the document to `POST /try/dry-run`, which:
  - checks the graph (references, explicit dependencies, cycles);
  - checks shell syntax with `bash -n` and Python syntax with `compile`;
  - builds the DAGonStar tasks in portable emulation (skipped when the workflow has Native tasks: guest code is never imported);
  - returns the execution plan: stages (tasks in the same stage run in parallel), what each task would do, the files it would stage in, and the command or `DagonTask` call it would run.
- The editor plays the plan back on the canvas (stage by stage, or at once with reduced motion) and shows it in a side panel. **Nothing is executed** and nothing is written to the scratch directory or the database.
- Limits: 512 KB and 60 tasks per dry run.

Signed-in users get the same **Dry run** button next to **▶ Run** in the editor.

Administrators can turn the playground off with **Admin → Authentication → Let visitors try DAGonWeb without an account** (`DAGONWEB_GUEST_PLAYGROUND_ENABLED=false`). Anonymous visitors are then sent to the sign-in page.
