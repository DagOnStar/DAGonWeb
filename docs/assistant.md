# Workflow assistant (LLM agents)

When an administrator enables the **LLM connector** (Admin → LLM connector), users with the `assistant.use` permission get a **✨ Assistant** panel in the editor and a **✨ Design with assistant** button on the workflows page.

The connector speaks the OpenAI Chat Completions API with tool calls, so it works with OpenAI, Azure OpenAI, and self-hosted OpenAI-compatible servers such as vLLM or Ollama (`http://ollama:11434/v1`). The API key can also come from `OPENAI_API_KEY`.

## How it works

You describe what you need ("download a CSV, compute the column means and draw a bar chart") or ask for a change ("make the training step a Slurm job"). The agent works on a **draft** copy of the workflow with these tools:

| Tool | Purpose |
| --- | --- |
| `list_task_types` | The task-type registry: fields, defaults and documentation. |
| `get_workflow` / `set_workflow` | Read or replace the whole draft document. |
| `upsert_task` / `remove_task` | Add, change or delete one task. |
| `validate_workflow` | Static checks and a **dry run**: graph and reference checks, `bash -n` on shell commands, compiling Python, and building the DAGonStar runtime workflow in portable emulation. Nothing is executed. |
| `diagnose_last_run` | Statuses and log tails of the workflow's latest run, to explain or fix a failure. |

The loop stops after **Maximum agent steps per message** model calls. The panel shows the reply, a trace of the tool calls, and the draft with its dry-run result. Nothing changes on the canvas until you press **Apply to canvas**, which saves the draft through the normal editor API (so the usual validation and ownership checks apply). **Check workflow** runs the same dry run on demand, even without an LLM.

Drafts are always DAGonStar task-map documents, so anything the assistant produces can be exported as JSON, CWL or a Python application.

## Privacy

The workflow document and your messages are sent to the configured endpoint. Use a self-hosted model when workflows contain sensitive data. Run files are never sent; `diagnose_last_run` sends only log tails.
