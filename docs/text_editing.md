# Text editing (Monaco)

Every multi-line text field in DAGonWeb uses the [Monaco editor](https://microsoft.github.io/monaco-editor/), the editor of VS Code. It gives syntax highlighting, completion, hover help, inline problems, find and replace (Ctrl/⌘+F), multi-cursor editing, bracket matching and folding.

The fields are still plain `<textarea>` elements in the HTML. `app/static/js/code-editor.js` upgrades them when the page loads. The textarea stays in the form, hidden, and always holds the current text, so forms submit as before. If Monaco cannot be loaded (offline, or the CDN is blocked), the plain textareas are used instead.

## Where it is used

| Place | Language | Context-aware features |
| --- | --- | --- |
| Inspector: Batch, Slurm, Docker… commands | Bash | `$input` variables from *Named inputs*, common commands, snippets (`strict`, `for`, `heredoc`, `outputs`…), `workflow:///` references |
| Inspector: Native *Python source* | Python | keywords, built-ins, module members (`json.`, `os.path.`…), snippets, the task's input and output names, a `def <callable>(inputs…)` snippet |
| Inspector: JSON fields and *Raw configuration JSON* | JSON | validation and completion from a JSON schema: the raw configuration schema comes from the task-type registry, and fields such as the LLM *Request JSON*, *Form fields* and *Expected status* have their own schema |
| Inspector: `name = value` mappings | DAGonWeb mapping | per-field key suggestions (HTTP headers, environment variables, response outputs), syntax errors that mirror the server-side parser, invalid input names |
| Inspector: lists | DAGonWeb list | per-field suggestions (for example `sbatch` options) |
| Inspector: *requirements.txt* | pip requirements | common package names, version pins |
| Inspector: instructions, assistant prompt | plain text / Markdown | task names of the open workflow |
| Admin → dagon.ini, workflow *Advanced dagon.ini* | INI | section names (including `[llm.<name>]`, `[agent.<name>]`), the keys of the current section, values (`True`/`False`, log levels…), hover descriptions, syntax warnings |
| Workflow description, FAIR creators, admin text settings, run-page form fields | Markdown / plain text | word completion |
| Run page: live log, task log, file previews | log, or by file extension | read-only, with highlighting of errors and successes, folding and search |

In every editor, `workflow:///` references are completed in two steps: first the task names, then the files that task is known to produce. References are underlined, the hover tells which task produces the file, and a reference to a task that does not exist is flagged as a warning.

## Keyboard

| Keys | Action |
| --- | --- |
| Ctrl/⌘+Space | Show suggestions |
| Ctrl/⌘+Enter | Apply (Inspector), send (assistant) or submit the form |
| Ctrl/⌘+F, Ctrl/⌘+H | Find, replace |
| F1 | Command palette |
| Alt+F1 | Monaco's accessibility help |
| Ctrl+M (Ctrl+Shift+M on macOS) | Switch Tab between indenting and moving focus |

In prose fields (plain text, Markdown) Tab moves to the next field by default. In code fields Tab indents until you press Ctrl+M.

## For developers

- Mark a textarea with `data-code-language="<language>"` (`shell`, `python`, `json`, `ini`, `markdown`, `plaintext`, `yaml`, `dagon-mapping`, `dagon-list`, `pip-requirements`, `dagon-log`) to upgrade it. Optional attributes are `data-code-min-lines`, `data-code-max-lines` and `data-code-suggestions` (a JSON list). A `<pre data-code-language="…">` becomes a read-only view.
- Content inserted later is upgraded with `DAGonWebCode.enhance(container)`. Editors whose element has been removed are disposed automatically.
- `DAGonWebCode.attach(textarea, options)` accepts `language`, `context` (a function that returns `{tasks: [{name, type, files}], inputs, outputs, current, function}`), `schema` (a JSON schema), `suggestions`, `onSubmit`, `minLines` and `maxLines`.
- Inspector fields take their editor hints from the registry in `app/task_specs.py`: `language`, `suggestions` and `schema` on a field. Add hints there, not in the JavaScript.
- Monaco is loaded from `cdnjs.cloudflare.com` (listed in `THIRD_PARTY_ASSETS` in `app/privacy.py`). It stores nothing in the browser.
