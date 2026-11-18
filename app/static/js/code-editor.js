/* DAGonWeb text editing with the Monaco editor.
 *
 * Every multi-line text field in DAGonWeb is a plain <textarea> in the HTML.
 * This script upgrades it to a Monaco editor (progressive enhancement): the
 * textarea stays in the form, hidden, and always holds the current text, so
 * forms, FormData and the existing scripts keep working. If Monaco cannot be
 * loaded (offline, blocked CDN) the textarea is simply left as it is.
 *
 * Declarative use:   <textarea data-code-language="ini">…</textarea>
 *                    <pre data-code-language="json">…</pre>   (read-only view)
 * Programmatic use:  DAGonWebCode.attach(textarea, { language, context, schema, … })
 *
 * Context-sensitive features, on top of Monaco's own highlighting, folding,
 * find/replace, bracket matching and multi-cursor editing:
 *   - workflow:///task/path completion, hover and "unknown task" warnings,
 *     fed by the workflow editor (task names and the files they produce);
 *   - shell: $input variables, common commands and snippets;
 *   - Python: keywords, built-ins, snippets and the task's input names;
 *   - JSON: validation and completion from a JSON schema derived from the
 *     task-type registry (app/task_specs.py);
 *   - dagon.ini: sections, keys, values and descriptions;
 *   - "name = value" mappings and lists: per-field suggestions from the
 *     registry, and syntax checking that mirrors the server-side parser.
 * Keyboard: Ctrl/⌘+Space suggests, Ctrl/⌘+Enter submits, F1 opens the command
 * palette, Alt+F1 shows Monaco's accessibility help, and Ctrl+M
 * (Ctrl+Shift+M on macOS) toggles whether Tab indents or moves focus.
 */
(() => {
  'use strict';

  const VERSION = '0.52.2';
  const BASE = `https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/${VERSION}/min`;
  // Mirrors app.task_specs.WORKFLOW_REFERENCE; quotes and commas end a reference in raw text.
  const REFERENCE_RE = /workflow:\/\/([A-Za-z0-9_-]{0,80})\/([A-Za-z0-9_-]{1,80})\/([^\s;|&<>()"'`,]+)/g;
  const PROSE = new Set(['plaintext', 'markdown']);
  const LANGUAGES = ['shell', 'python', 'json', 'ini', 'markdown', 'plaintext', 'yaml', 'dagon-mapping', 'dagon-list', 'pip-requirements', 'dagon-log'];
  const EXTENSIONS = {
    py: 'python', sh: 'shell', bash: 'shell', zsh: 'shell', json: 'json', geojson: 'json', ipynb: 'json', ini: 'ini', cfg: 'ini', toml: 'ini', env: 'ini',
    md: 'markdown', markdown: 'markdown', yaml: 'yaml', yml: 'yaml', cwl: 'yaml', xml: 'xml', html: 'html', htm: 'html', svg: 'xml', css: 'css',
    js: 'javascript', ts: 'typescript', sql: 'sql', r: 'r', log: 'dagon-log', out: 'dagon-log', err: 'dagon-log', txt: 'plaintext', csv: 'plaintext', tsv: 'plaintext',
    dockerfile: 'dockerfile', c: 'c', h: 'c', cpp: 'cpp', f90: 'plaintext', java: 'java', go: 'go', rs: 'rust', jl: 'julia',
  };

  let loading = null;
  let monaco = null;
  let counter = 0;
  const handles = new Set();
  const byModel = new Map();
  const schemas = new Map();

  // ------------------------------------------------------------------ loading
  function load() {
    if (loading) return loading;
    // Monaco cancels pending work (hover, word highlight) when an editor is
    // disposed; those rejections are expected and not errors.
    window.addEventListener('unhandledrejection', event => { if (event.reason?.name === 'Canceled') event.preventDefault(); });
    loading = new Promise((resolve, reject) => {
      if (window.monaco?.editor) { resolve(window.monaco); return; }
      const worker = `self.MonacoEnvironment={baseUrl:'${BASE}/'};importScripts('${BASE}/vs/base/worker/workerMain.js');`;
      window.MonacoEnvironment = { getWorkerUrl: () => `data:text/javascript;charset=utf-8,${encodeURIComponent(worker)}` };
      const script = document.createElement('script');
      script.src = `${BASE}/vs/loader.min.js`;
      script.crossOrigin = 'anonymous';
      script.referrerPolicy = 'no-referrer';
      script.onload = () => {
        window.require.config({ paths: { vs: `${BASE}/vs` } });
        window.require(['vs/editor/editor.main'], () => resolve(window.monaco), reject);
      };
      script.onerror = () => reject(new Error('Monaco could not be loaded'));
      document.head.append(script);
    }).then(instance => { monaco = instance; setup(); return instance; }).catch(error => {
      console.warn('DAGonWeb: the Monaco editor is not available, plain text fields are used instead.', error);
      return null;
    });
    return loading;
  }

  const darkMode = () => document.documentElement.getAttribute('data-bs-theme') === 'dark';
  const theme = () => (darkMode() ? 'dagonweb-dark' : 'dagonweb-light');
  const optionsOf = model => byModel.get(model.uri.toString()) || {};
  function contextOf(model) {
    const options = optionsOf(model);
    try { return (typeof options.context === 'function' ? options.context() : options.context) || null; } catch { return null; }
  }

  // -------------------------------------------------------- one-time setup
  function setup() {
    const { languages, editor } = monaco;
    // Contrast: every custom token colour is at least 4.5:1 on the editor background.
    editor.defineTheme('dagonweb-light', {
      base: 'vs', inherit: true,
      rules: [
        { token: 'reference', foreground: '0a58ca', fontStyle: 'underline' },
        { token: 'key', foreground: '6f42c1' },
        { token: 'option', foreground: '0a58ca' },
        { token: 'error', foreground: 'b02a37', fontStyle: 'bold' },
        { token: 'ok', foreground: '146c43' },
        { token: 'warning', foreground: '8a5300' },
        { token: 'timestamp', foreground: '5c636a' },
      ],
      colors: { 'editor.background': '#ffffff', 'editorLineNumber.foreground': '#5c636a' },
    });
    editor.defineTheme('dagonweb-dark', {
      base: 'vs-dark', inherit: true,
      rules: [
        { token: 'reference', foreground: '6ea8fe', fontStyle: 'underline' },
        { token: 'key', foreground: 'c29ffa' },
        { token: 'option', foreground: '6ea8fe' },
        { token: 'error', foreground: 'f1aeb5', fontStyle: 'bold' },
        { token: 'ok', foreground: '75b798' },
        { token: 'warning', foreground: 'ffda6a' },
        { token: 'timestamp', foreground: 'adb5bd' },
      ],
      colors: { 'editor.background': '#1b1f23', 'editorLineNumber.foreground': '#9aa0a6' },
    });
    new MutationObserver(() => editor.setTheme(theme())).observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] });

    registerLanguages();
    LANGUAGES.forEach(id => {
      languages.registerCompletionItemProvider(id, { triggerCharacters: ['/', ':'], provideCompletionItems: referenceCompletions });
      languages.registerHoverProvider(id, { provideHover: referenceHover });
    });
    languages.registerCompletionItemProvider('shell', { triggerCharacters: ['$', '{'], provideCompletionItems: shellCompletions });
    languages.registerCompletionItemProvider('python', { triggerCharacters: ['.'], provideCompletionItems: pythonCompletions });
    languages.registerCompletionItemProvider('ini', { triggerCharacters: ['[', '='], provideCompletionItems: iniCompletions });
    languages.registerHoverProvider('ini', { provideHover: iniHover });
    languages.registerCompletionItemProvider('dagon-mapping', { triggerCharacters: ['='], provideCompletionItems: mappingCompletions });
    languages.registerCompletionItemProvider('dagon-list', { triggerCharacters: ['-'], provideCompletionItems: listCompletions });
    languages.registerCompletionItemProvider('pip-requirements', { provideCompletionItems: requirementCompletions });
    ['markdown', 'plaintext'].forEach(id => languages.registerCompletionItemProvider(id, { provideCompletionItems: proseCompletions }));
  }

  function registerLanguages() {
    const { languages } = monaco;
    const reference = [/workflow:\/\/[A-Za-z0-9_-]*\/[A-Za-z0-9_-]+\/[^\s;|&<>()"'`,]+/, 'reference'];
    languages.register({ id: 'dagon-mapping' });
    languages.setMonarchTokensProvider('dagon-mapping', {
      tokenizer: {
        root: [
          [/^\s*#.*$/, 'comment'],
          [/^\s*[A-Za-z_][\w.-]*(?=\s*=)/, 'key'],
          [/=/, 'delimiter'],
          reference,
          [/\b(true|false|null)\b/, 'keyword'],
          [/-?\d+(\.\d+)?\b/, 'number'],
        ],
      },
    });
    languages.setLanguageConfiguration('dagon-mapping', { comments: { lineComment: '#' } });

    languages.register({ id: 'dagon-list' });
    languages.setMonarchTokensProvider('dagon-list', {
      tokenizer: { root: [[/^\s*#.*$/, 'comment'], reference, [/--?[A-Za-z][\w-]*/, 'option'], [/=/, 'delimiter'], [/-?\d+(\.\d+)?/, 'number']] },
    });
    languages.setLanguageConfiguration('dagon-list', { comments: { lineComment: '#' } });

    languages.register({ id: 'pip-requirements' });
    languages.setMonarchTokensProvider('pip-requirements', {
      tokenizer: {
        root: [[/#.*$/, 'comment'], [/^\s*-[\w-]+/, 'option'], [/^\s*[A-Za-z0-9][\w.-]*/, 'key'], [/\[[^\]]*\]/, 'type'], [/[<>=!~]=?|===/, 'operator'], [/[\d][\w.*+!-]*/, 'number'], [/;.*$/, 'comment']],
      },
    });
    languages.setLanguageConfiguration('pip-requirements', { comments: { lineComment: '#' } });

    languages.register({ id: 'dagon-log' });
    languages.setMonarchTokensProvider('dagon-log', {
      ignoreCase: true,
      tokenizer: {
        root: [
          [/.*\b(failed|failure|error|exception|traceback|fatal)\b.*/, 'error'],
          [/.*(completed with status: success|completed\.|succeeded).*/, 'ok'],
          [/.*\b(warning|warn)\b.*/, 'warning'],
          [/\d{4}-\d\d-\d\d[ T][\d:.,]+/, 'timestamp'],
          [/\b(DEBUG|INFO|NOTICE)\b/, 'keyword'],
          reference,
        ],
      },
    });
  }

  // ------------------------------------------------------- completion helpers
  function item(range, label, kind, insertText, extra = {}) {
    return { label, kind: monaco.languages.CompletionItemKind[kind], insertText: insertText ?? label, range, ...extra };
  }
  function snippet(range, label, body, documentation) {
    return item(range, label, 'Snippet', body, { insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet, documentation, sortText: `~${label}` });
  }
  function wordRange(model, position) {
    const word = model.getWordUntilPosition(position);
    return new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
  }
  const before = (model, position) => model.getLineContent(position.lineNumber).slice(0, position.column - 1);
  const retrigger = { id: 'editor.action.triggerSuggest', title: 'Suggest' };
  const knownTasks = context => (context?.tasks || []);

  // workflow:/// references: task names, then the files that task produces.
  function referenceCompletions(model, position) {
    const context = contextOf(model);
    if (!context) return { suggestions: [] };
    const text = before(model, position);
    const line = position.lineNumber;
    let match = text.match(/workflow:\/\/([A-Za-z0-9_-]*)\/([A-Za-z0-9_-]+)\/([^\s;|&<>()"'`,]*)$/);
    if (match) {
      const task = knownTasks(context).find(candidate => candidate.name === match[2]);
      const range = new monaco.Range(line, position.column - match[3].length, line, position.column);
      return {
        suggestions: (task?.files || []).map(file => item(range, file, 'File', file, { detail: `produced by ${task.name}`, sortText: `0${file}` }))
          .concat(item(range, 'outputs/', 'Folder', 'outputs/', { detail: 'task outputs folder', sortText: '1', command: retrigger })),
      };
    }
    match = text.match(/workflow:\/\/([A-Za-z0-9_-]*)\/([A-Za-z0-9_-]*)$/);
    if (match) {
      const range = new monaco.Range(line, position.column - match[2].length, line, position.column);
      return {
        suggestions: knownTasks(context).map(task => item(range, task.name, 'Module', `${task.name}/`, {
          detail: task.label || task.type, documentation: task.files?.length ? `Produces: ${task.files.join(', ')}` : 'No known outputs yet', command: retrigger,
        })),
      };
    }
    match = text.match(/(?:^|[^A-Za-z0-9_])(w[a-z:/]{0,11})$/);
    if (match && 'workflow:///'.startsWith(match[1])) {
      const range = new monaco.Range(line, position.column - match[1].length, line, position.column);
      const references = knownTasks(context).flatMap(task => (task.files || []).map(file => `workflow:///${task.name}/${file}`));
      return {
        suggestions: [
          item(range, 'workflow:///', 'Keyword', 'workflow:///', { detail: 'reference to a file of another task', command: retrigger, sortText: '0' }),
          ...references.map(reference => item(range, reference, 'Reference', reference, { detail: 'data dependency (DAGonStar stages the file)', sortText: `1${reference}` })),
        ],
      };
    }
    return { suggestions: [] };
  }

  function referenceAt(model, position) {
    const text = model.getLineContent(position.lineNumber);
    for (const match of text.matchAll(REFERENCE_RE)) {
      const start = match.index + 1;
      if (position.column >= start && position.column <= start + match[0].length) return { match, range: new monaco.Range(position.lineNumber, start, position.lineNumber, start + match[0].length) };
    }
    return null;
  }

  function referenceHover(model, position) {
    const found = referenceAt(model, position);
    if (!found) return null;
    const [, workflow, taskName, path] = found.match;
    const context = contextOf(model);
    const task = knownTasks(context).find(candidate => candidate.name === taskName);
    const lines = [`**Data dependency** on task \`${taskName}\`${workflow ? ` of workflow \`${workflow}\`` : ''}`, `DAGonStar stages \`${path}\` into this task before it runs.`];
    if (context && !workflow) {
      if (!task) lines.push('⚠ No task with this name exists in the workflow.');
      else if (task.files?.length && !task.files.includes(path)) lines.push(`Known outputs of \`${taskName}\`: ${task.files.map(file => `\`${file}\``).join(', ')}`);
    }
    return { range: found.range, contents: lines.map(value => ({ value })) };
  }

  const SHELL_COMMANDS = ['awk', 'basename', 'cat', 'cd', 'chmod', 'cp', 'curl', 'cut', 'date', 'dirname', 'echo', 'env', 'export', 'find', 'grep', 'gunzip', 'gzip', 'head', 'jq', 'ln', 'ls', 'mkdir -p', 'mv', 'paste', 'printf', 'python3', 'Rscript', 'rm', 'sed', 'sleep', 'sort', 'tail', 'tar', 'tee', 'test', 'touch', 'tr', 'uniq', 'unzip', 'wc', 'wget', 'xargs', 'zip'];
  function shellCompletions(model, position) {
    const context = contextOf(model) || {};
    const text = before(model, position);
    const line = position.lineNumber;
    const variable = text.match(/\$\{?([A-Za-z_][A-Za-z0-9_]*)?$/);
    const inputs = context.inputs || [];
    if (variable) {
      const braced = text.slice(-1 - (variable[1] || '').length).startsWith('{');
      const range = new monaco.Range(line, position.column - (variable[1] || '').length, line, position.column);
      return {
        suggestions: [
          ...inputs.map(name => item(range, name, 'Variable', braced ? `${name}}` : name, { detail: 'named input: staged path', sortText: `0${name}` })),
          ...['PWD', 'HOME', 'PATH', 'USER', 'TMPDIR'].map(name => item(range, name, 'Variable', braced ? `${name}}` : name, { detail: 'environment', sortText: `1${name}` })),
        ],
      };
    }
    if (/workflow:\/\/\S*$/.test(text)) return { suggestions: [] };
    const range = wordRange(model, position);
    return {
      suggestions: [
        ...SHELL_COMMANDS.map(command => item(range, command, 'Function')),
        ...inputs.map(name => item(range, `$${name}`, 'Variable', `"$${name}"`, { detail: 'named input: staged path', filterText: name })),
        snippet(range, 'outputs', 'mkdir -p outputs\n$0', 'Create the outputs folder that downstream tasks reference.'),
        snippet(range, 'strict', 'set -euo pipefail\n$0', 'Stop on the first error.'),
        snippet(range, 'if', 'if [ ${1:-f "$file"} ]; then\n\t$0\nfi', 'if statement'),
        snippet(range, 'for', 'for ${1:item} in ${2:*}; do\n\t$0\ndone', 'for loop'),
        snippet(range, 'while', 'while read -r ${1:line}; do\n\t$0\ndone < ${2:file}', 'read a file line by line'),
        snippet(range, 'heredoc', "cat > ${1:outputs/file.txt} <<'${2:EOF}'\n$0\n${2:EOF}", 'write a file from inline text'),
        snippet(range, 'function', '${1:name}() {\n\t$0\n}', 'shell function'),
        snippet(range, 'python', "python3 - <<'PY'\n$0\nPY", 'inline Python'),
      ],
    };
  }

  const PY_KEYWORDS = ['and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'False', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'None', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'True', 'try', 'while', 'with', 'yield'];
  const PY_BUILTINS = ['abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'filter', 'float', 'format', 'getattr', 'hasattr', 'int', 'isinstance', 'len', 'list', 'map', 'max', 'min', 'open', 'print', 'range', 'repr', 'reversed', 'round', 'set', 'setattr', 'sorted', 'str', 'sum', 'tuple', 'type', 'zip'];
  const PY_MODULES = { json: ['dump', 'dumps', 'load', 'loads'], os: ['environ', 'getcwd', 'listdir', 'makedirs', 'path'], 'os.path': ['basename', 'dirname', 'exists', 'join', 'splitext'], pathlib: ['Path'], csv: ['DictReader', 'DictWriter', 'reader', 'writer'], math: ['ceil', 'floor', 'log', 'pi', 'sqrt'], statistics: ['mean', 'median', 'stdev'], sys: ['argv', 'exit', 'stderr', 'stdout'] };
  function pythonCompletions(model, position) {
    const context = contextOf(model) || {};
    const text = before(model, position);
    const range = wordRange(model, position);
    const member = text.match(/([A-Za-z_][\w.]*)\.\w*$/);
    if (member && PY_MODULES[member[1]]) return { suggestions: PY_MODULES[member[1]].map(name => item(range, name, 'Function', name, { detail: member[1] })) };
    if (member) return { suggestions: [] };
    const inputs = context.inputs || [];
    const outputs = context.outputs || [];
    const fn = context.function || 'main';
    const params = [...inputs, ...outputs];
    return {
      suggestions: [
        ...PY_KEYWORDS.map(word => item(range, word, 'Keyword')),
        ...PY_BUILTINS.map(name => item(range, name, 'Function')),
        ...Object.keys(PY_MODULES).filter(name => !name.includes('.')).map(name => item(range, name, 'Module')),
        ...inputs.map(name => item(range, name, 'Variable', name, { detail: 'function input', sortText: `0${name}` })),
        ...outputs.map(name => item(range, name, 'Variable', name, { detail: 'declared output path', sortText: `0${name}` })),
        snippet(range, `def ${fn}`, `def ${fn}(${params.map((name, index) => `\${${index + 1}:${name}}`).join(', ')}):\n\t"""\${${params.length + 1}:Task entry point.}"""\n\t$0`, 'Function called by DAGonStar with the task inputs and outputs.'),
        snippet(range, 'def', 'def ${1:name}(${2}):\n\t$0', 'function'),
        snippet(range, 'class', 'class ${1:Name}:\n\tdef __init__(self${2}):\n\t\t$0', 'class'),
        snippet(range, 'main', 'if __name__ == "__main__":\n\t$0', 'script entry point'),
        snippet(range, 'with open', 'with open(${1:path}, "${2:r}", encoding="utf-8") as ${3:handle}:\n\t$0', 'open a file'),
        snippet(range, 'try', 'try:\n\t$1\nexcept ${2:Exception} as ${3:error}:\n\t$0', 'try/except'),
        snippet(range, 'for', 'for ${1:item} in ${2:items}:\n\t$0', 'for loop'),
        snippet(range, 'read json', 'with open(${1:path}, encoding="utf-8") as handle:\n\t${2:data} = json.load(handle)\n$0', 'load a JSON file'),
        snippet(range, 'write json', 'with open(${1:path}, "w", encoding="utf-8") as handle:\n\tjson.dump(${2:data}, handle, indent=2)\n$0', 'write a JSON file'),
        snippet(range, 'pandas csv', 'import pandas as pd\n\n${1:frame} = pd.read_csv(${2:path})\n$0', 'read a CSV with pandas'),
      ],
    };
  }

  const INI_SCHEMA = {
    dagon_service: { route: 'URL of the DAGonStar service, for example http://localhost:57000.', use: 'True to register runs with the DAGonStar service; False runs in process.' },
    ftp_pub: { ip: 'Public address of the FTP server used to stage files between sites.' },
    dagon_ip: { ip: 'Address other sites use to reach this host.' },
    batch: { scratch_dir_base: 'Base folder of task working directories. DAGonWeb always replaces it with its own scratch root.', run_base: 'Optional prefix for run folders.', threads: 'How many batch tasks run at the same time.', remove_dir: 'Remove task folders after the run. DAGonWeb always forces False.' },
    slurm: { partition: 'Default SLURM partition.' },
    ec2: { key: 'AWS access key id.', secret: 'AWS secret access key.', region: 'AWS region, for example eu-west-1.' },
    digitalocean: { key: 'DigitalOcean API token.' },
    gce: { key: 'Google Cloud service account e-mail.', secret: 'Path to the service account key file.', project: 'Google Cloud project id.' },
    loggers: { keys: 'Comma-separated logger names.' },
    handlers: { keys: 'Comma-separated handler names.' },
    formatters: { keys: 'Comma-separated formatter names.' },
    logger_root: { level: 'Root log level.', handlers: 'Handlers of the root logger.' },
    handler_stream_handler: { class: 'Python logging handler class.', level: 'Handler log level.', formatter: 'Formatter name.', args: 'Handler constructor arguments, for example (sys.stderr,).' },
    formatter_formatter: { format: 'Python logging format string.' },
    'llm.*': { endpoint: 'OpenAI-compatible endpoint URL.', api_key_env: 'Environment variable that holds the API key (never put the key itself here).', model: 'Default model name.' },
    'agent.*': { provider: 'Name of an [agent_provider.<name>] section.', strategy: 'Agent strategy, for example bounded_iterative.', tools: 'Comma-separated tool names.', guardrails: 'Comma-separated guardrail names.', max_steps: 'Maximum reasoning steps.', max_tool_calls: 'Maximum tool calls.', timeout_seconds: 'Time limit in seconds.', max_tokens: 'Token budget.', max_tool_output_bytes: 'Largest tool output kept.', max_trace_bytes: 'Largest trace kept.' },
    'agent_provider.*': { implementation: 'Provider implementation, for example mock or openai.', endpoint: 'Endpoint URL.', api_key_env: 'Environment variable that holds the API key.', model: 'Model name.' },
  };
  const INI_VALUES = { use: ['True', 'False'], remove_dir: ['False', 'True'], level: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], class: ['StreamHandler', 'FileHandler'], implementation: ['mock', 'openai'], strategy: ['bounded_iterative'] };
  const iniKeys = section => INI_SCHEMA[section] || INI_SCHEMA[`${String(section).split('.')[0]}.*`] || null;
  function iniSectionAt(model, lineNumber) {
    for (let line = lineNumber; line >= 1; line -= 1) {
      const match = model.getLineContent(line).trim().match(/^\[([^\]]+)\]/);
      if (match) return match[1];
    }
    return null;
  }

  function iniCompletions(model, position) {
    const text = before(model, position);
    const line = position.lineNumber;
    if (/^\s*\[[^\]]*$/.test(text)) {
      const start = text.indexOf('[') + 2;
      const range = new monaco.Range(line, start, line, model.getLineMaxColumn(line));
      const present = new Set(model.getLinesContent().map(value => (value.trim().match(/^\[([^\]]+)\]/) || [])[1]).filter(Boolean));
      return {
        suggestions: Object.keys(INI_SCHEMA).map(name => (name.endsWith('*')
          ? snippet(range, name.replace('*', '<name>'), `${name.replace('*', '${1:name}')}]`, `Add a [${name}] section.`)
          : item(range, name, 'Module', `${name}]`, { detail: present.has(name) ? 'already present' : 'DAGonStar section', sortText: present.has(name) ? `1${name}` : `0${name}` }))),
      };
    }
    const section = iniSectionAt(model, line);
    const keys = iniKeys(section) || {};
    const value = text.match(/^\s*([\w.-]+)\s*=\s*(\S*)$/);
    if (value) {
      const range = new monaco.Range(line, position.column - value[2].length, line, position.column);
      return { suggestions: (INI_VALUES[value[1]] || []).map(choice => item(range, choice, 'Value')) };
    }
    if (/^\s*[\w.-]*$/.test(text) && section) {
      const present = new Set();
      for (let current = line + 1; current <= model.getLineCount() && !/^\s*\[/.test(model.getLineContent(current)); current += 1) present.add(model.getLineContent(current).split('=')[0].trim());
      for (let current = line - 1; current >= 1 && !/^\s*\[/.test(model.getLineContent(current)); current -= 1) present.add(model.getLineContent(current).split('=')[0].trim());
      const range = wordRange(model, position);
      return { suggestions: Object.entries(keys).filter(([key]) => !present.has(key)).map(([key, doc]) => item(range, key, 'Property', `${key} = `, { documentation: doc, command: INI_VALUES[key] ? retrigger : undefined })) };
    }
    return { suggestions: [] };
  }

  function iniHover(model, position) {
    const text = model.getLineContent(position.lineNumber);
    const section = text.trim().match(/^\[([^\]]+)\]/);
    if (section) return { contents: [{ value: iniKeys(section[1]) ? `**[${section[1]}]** — DAGonStar section with keys: ${Object.keys(iniKeys(section[1])).map(key => `\`${key}\``).join(', ')}` : `**[${section[1]}]** — custom section` }] };
    const key = text.match(/^\s*([\w.-]+)\s*=/);
    if (!key || position.column > key[0].length) return null;
    const doc = (iniKeys(iniSectionAt(model, position.lineNumber)) || {})[key[1]];
    return doc ? { contents: [{ value: `**${key[1]}** — ${doc}` }] } : null;
  }

  function mappingCompletions(model, position) {
    const options = optionsOf(model);
    const text = before(model, position);
    const range = wordRange(model, position);
    if (!text.includes('=')) {
      const used = new Set(model.getLinesContent().map(value => value.split('=')[0].trim()));
      return { suggestions: (options.suggestions || []).filter(key => !used.has(key)).map(key => item(range, key, 'Property', `${key} = `, { command: retrigger })) };
    }
    if (options.field === 'outputs' && /=\s*\S*$/.test(text)) return { suggestions: [item(range, 'outputs/', 'Folder', 'outputs/')] };
    return { suggestions: [] };
  }

  function listCompletions(model, position) {
    const range = wordRange(model, position);
    const text = before(model, position).trimStart();
    const start = position.column - text.length;
    const lineRange = new monaco.Range(position.lineNumber, start, position.lineNumber, position.column);
    return { suggestions: (optionsOf(model).suggestions || []).map(value => item(value.startsWith('-') ? lineRange : range, value, 'Value', value)) };
  }

  const PACKAGES = ['numpy', 'pandas', 'scipy', 'matplotlib', 'requests', 'xarray', 'netCDF4', 'scikit-learn', 'pyyaml', 'shapely', 'geopandas', 'rasterio', 'dask', 'pyarrow', 'openpyxl', 'jinja2', 'pillow', 'sympy', 'statsmodels', 'seaborn', 'plotly', 'boto3', 'tqdm', 'pydantic'];
  function requirementCompletions(model, position) {
    if (!/^\s*[\w.-]*$/.test(before(model, position))) return { suggestions: [] };
    const range = wordRange(model, position);
    return { suggestions: [...PACKAGES.map(name => item(range, name, 'Module')), snippet(range, 'pin', '${1:package}==${2:1.0}', 'pin an exact version')] };
  }

  function proseCompletions(model, position) {
    const options = optionsOf(model);
    const range = wordRange(model, position);
    const tasks = knownTasks(contextOf(model)).map(task => item(range, task.name, 'Module', task.name, { detail: task.label || task.type }));
    return { suggestions: [...tasks, ...(options.suggestions || []).map(value => item(range, value, 'Text'))] };
  }

  // ------------------------------------------------------------- diagnostics
  function validate(handle) {
    const { model, options } = handle;
    const markers = [];
    const context = contextOf(model);
    const lines = model.getLinesContent();
    const names = context ? new Set(knownTasks(context).map(task => task.name)) : null;
    const decorations = [];
    lines.forEach((text, index) => {
      for (const match of text.matchAll(REFERENCE_RE)) {
        const range = { startLineNumber: index + 1, startColumn: match.index + 1, endLineNumber: index + 1, endColumn: match.index + 1 + match[0].length };
        decorations.push({ range, options: { inlineClassName: 'dw-code-ref' } });
        if (names && !match[1] && !names.has(match[2]) && match[2] !== context.current) {
          markers.push({ ...range, severity: monaco.MarkerSeverity.Warning, message: `No task named "${match[2]}" in this workflow.` });
        } else if (names && match[2] === context.current) {
          markers.push({ ...range, severity: monaco.MarkerSeverity.Error, message: 'A task cannot reference its own outputs.' });
        }
      }
      if (model.getLanguageId() === 'dagon-mapping') {
        const trimmed = text.trim();
        if (trimmed && !trimmed.startsWith('#') && text.indexOf('=') < 1) markers.push({ startLineNumber: index + 1, startColumn: 1, endLineNumber: index + 1, endColumn: text.length + 1, severity: monaco.MarkerSeverity.Error, message: `${options.label || 'Mapping'}: use one "name = value" per line.` });
        else if (trimmed && !trimmed.startsWith('#') && !/^[A-Za-z_][\w.-]*$/.test(text.slice(0, text.indexOf('=')).trim()) && options.strictKeys) markers.push({ startLineNumber: index + 1, startColumn: 1, endLineNumber: index + 1, endColumn: text.indexOf('=') + 1, severity: monaco.MarkerSeverity.Error, message: 'Names must start with a letter or _ and use letters, digits and _.' });
      }
      if (model.getLanguageId() === 'ini') {
        const trimmed = text.trim();
        if (trimmed && !/^[;#]/.test(trimmed) && !/^\[[^\]]+\]$/.test(trimmed) && !text.includes('=') && !/^\s/.test(text)) markers.push({ startLineNumber: index + 1, startColumn: 1, endLineNumber: index + 1, endColumn: text.length + 1, severity: monaco.MarkerSeverity.Warning, message: 'Expected [section] or key = value.' });
      }
    });
    monaco.editor.setModelMarkers(model, 'dagonweb', markers);
    handle.decorations.set(decorations);
  }

  function refreshSchemas() {
    const json = monaco.languages.json;
    if (!json) return;
    json.jsonDefaults.setDiagnosticsOptions({
      validate: true, allowComments: false, enableSchemaRequest: false,
      schemas: [...schemas.entries()].map(([uri, schema]) => ({ uri: `${uri}.schema`, fileMatch: [uri], schema })),
    });
  }

  // ---------------------------------------------------------------- attaching
  const nativeValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');

  function labelText(textarea) {
    if (textarea.getAttribute('aria-label')) return textarea.getAttribute('aria-label');
    const label = textarea.id && document.querySelector(`label[for="${CSS.escape(textarea.id)}"]`);
    return (label?.textContent || textarea.name || 'Text').trim();
  }

  function sweep() {
    handles.forEach(handle => { if (!handle.host.isConnected) handle.dispose(); });
  }

  /**
   * Upgrade a textarea to Monaco. Resolves to a handle, or null when Monaco is
   * not available (the textarea then stays as it is).
   */
  async function attach(textarea, options = {}) {
    if (!textarea || textarea.dataset.codeAttached) return textarea?.codeEditor || null;
    textarea.dataset.codeAttached = 'pending';
    const instance = await load();
    if (!instance || !textarea.isConnected) { delete textarea.dataset.codeAttached; return null; }
    sweep();
    const data = textarea.dataset;
    const language = options.language || data.codeLanguage || 'plaintext';
    const prose = options.prose ?? (data.codeProse ? data.codeProse === 'true' : PROSE.has(language));
    const readOnly = options.readOnly ?? (textarea.disabled || textarea.readOnly);
    const minLines = Number(options.minLines ?? data.codeMinLines ?? textarea.rows ?? 3) || 3;
    const maxLines = Number(options.maxLines ?? data.codeMaxLines ?? Math.max(minLines, 24));
    const suggestions = options.suggestions ?? (data.codeSuggestions ? JSON.parse(data.codeSuggestions) : undefined);
    const label = options.label || labelText(textarea);
    const settings = { ...options, language, suggestions, label };

    const host = document.createElement('div');
    host.className = `dw-code${prose ? ' dw-code-prose' : ''}${textarea.classList.contains('is-invalid') ? ' is-invalid' : ''}${readOnly ? ' dw-code-readonly' : ''}`;
    textarea.after(host);
    textarea.classList.add('dw-code-source');
    textarea.setAttribute('aria-hidden', 'true');
    const previousTabIndex = textarea.getAttribute('tabindex');
    textarea.tabIndex = -1;

    counter += 1;
    const uri = monaco.Uri.parse(`inmemory://dagonweb/${counter}/${(textarea.id || textarea.name || 'text').replace(/[^\w.-]/g, '_')}`);
    const model = monaco.editor.createModel(textarea.value, language, uri);
    byModel.set(uri.toString(), settings);
    if (options.schema) { schemas.set(uri.toString(), options.schema); refreshSchemas(); }
    const editor = monaco.editor.create(host, {
      model, theme: theme(), automaticLayout: true, fixedOverflowWidgets: true,
      ariaLabel: label, accessibilitySupport: 'auto', readOnly, domReadOnly: readOnly,
      fontSize: 13, tabSize: language === 'python' ? 4 : 2, insertSpaces: true, detectIndentation: false,
      wordWrap: options.wordWrap || (prose || ['dagon-log', 'markdown', 'dagon-mapping'].includes(language) ? 'on' : 'off'),
      lineNumbers: prose || options.lineNumbers === false ? 'off' : 'on', lineNumbersMinChars: 3, lineDecorationsWidth: prose ? 4 : 8,
      folding: !prose, glyphMargin: false, minimap: { enabled: false }, overviewRulerLanes: 0, hideCursorInOverviewRuler: true,
      renderLineHighlight: prose || readOnly ? 'none' : 'line', scrollBeyondLastLine: false,
      scrollbar: { alwaysConsumeMouseWheel: false, verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
      padding: { top: 6, bottom: 6 }, tabFocusMode: prose, placeholder: textarea.placeholder || undefined,
      quickSuggestions: readOnly ? false : { other: true, comments: false, strings: true }, suggestOnTriggerCharacters: true,
      wordBasedSuggestions: prose ? 'currentDocument' : 'matchingDocuments', snippetSuggestions: 'inline', suggest: { showWords: true },
      stickyScroll: { enabled: false }, contextmenu: true, unicodeHighlight: { ambiguousCharacters: !prose },
    });

    // Keep the textarea (and therefore forms and FormData) in sync, in both directions.
    let silent = false;
    Object.defineProperty(textarea, 'value', {
      configurable: true,
      get: () => model.getValue(),
      set: value => {
        const text = String(value ?? '');
        nativeValue.set.call(textarea, text);
        if (text === model.getValue()) return;
        silent = true;
        try { model.pushEditOperations([], [{ range: model.getFullModelRange(), text }], () => null); } finally { silent = false; }
      },
    });
    const forwardFocus = () => editor.focus();
    textarea.focus = forwardFocus;
    textarea.addEventListener('focus', forwardFocus);
    textarea.setRangeText = text => handle.insert(text);
    const labels = textarea.id ? [...document.querySelectorAll(`label[for="${CSS.escape(textarea.id)}"]`)] : [];
    const onLabel = event => { event.preventDefault(); editor.focus(); };
    labels.forEach(element => element.addEventListener('click', onLabel));

    const input = host.querySelector('textarea');
    if (input) {
      ['aria-describedby', 'aria-invalid'].forEach(name => { if (textarea.hasAttribute(name)) input.setAttribute(name, textarea.getAttribute(name)); });
      if (textarea.required) input.setAttribute('aria-required', 'true');
    }

    const lineHeight = editor.getOption(monaco.editor.EditorOption.lineHeight);
    const fit = () => {
      const height = Math.min(maxLines * lineHeight + 12, Math.max(minLines * lineHeight + 12, editor.getContentHeight()));
      host.style.height = `${height}px`;
    };
    editor.onDidContentSizeChange(fit);
    fit();

    let validateTimer = null;
    const handle = {
      editor, model, textarea, host, options: settings, decorations: editor.createDecorationsCollection(),
      focus: () => editor.focus(),
      getValue: () => model.getValue(),
      setValue: text => { textarea.value = text; },
      insert(text) {
        const selection = editor.getSelection() || new monaco.Selection(1, 1, 1, 1);
        editor.executeEdits('dagonweb', [{ range: selection, text, forceMoveMarkers: true }]);
        editor.pushUndoStop();
        editor.focus();
      },
      revealEnd: () => editor.revealLine(model.getLineCount()),
      revalidate: () => validate(handle),
      dispose() {
        if (!handles.has(handle)) return;
        handles.delete(handle);
        clearTimeout(validateTimer);
        byModel.delete(uri.toString());
        if (schemas.delete(uri.toString())) refreshSchemas();
        const text = model.getValue();
        editor.dispose();
        model.dispose();
        host.remove();
        delete textarea.value;
        delete textarea.focus;
        delete textarea.setRangeText;
        delete textarea.codeEditor;
        textarea.value = text;
        textarea.removeEventListener('focus', forwardFocus);
        labels.forEach(element => element.removeEventListener('click', onLabel));
        textarea.classList.remove('dw-code-source');
        textarea.removeAttribute('aria-hidden');
        if (previousTabIndex === null) textarea.removeAttribute('tabindex'); else textarea.setAttribute('tabindex', previousTabIndex);
        delete textarea.dataset.codeAttached;
      },
    };
    model.onDidChangeContent(() => {
      nativeValue.set.call(textarea, model.getValue());
      clearTimeout(validateTimer);
      validateTimer = setTimeout(() => validate(handle), 250);
      if (!silent) textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    editor.onDidBlurEditorText(() => textarea.dispatchEvent(new Event('change', { bubbles: true })));
    editor.onDidFocusEditorText(() => textarea.dispatchEvent(new CustomEvent('dagonweb:code-focus', { bubbles: true, detail: handle })));
    const submit = options.onSubmit || (textarea.form ? () => textarea.form.requestSubmit() : null);
    if (submit && !readOnly) editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => submit(handle));
    (options.commands || []).forEach(command => editor.addCommand(command.keys(monaco), () => command.run(handle)));

    textarea.dataset.codeAttached = 'true';
    textarea.codeEditor = handle;
    handles.add(handle);
    validate(handle);
    return handle;
  }

  /** Read-only Monaco view of a <pre> (logs, file previews). */
  async function view(pre, options = {}) {
    if (!pre || pre.dataset.codeAttached) return pre?.codeEditor || null;
    pre.dataset.codeAttached = 'pending';
    const instance = await load();
    if (!instance || !pre.isConnected) { delete pre.dataset.codeAttached; return null; }
    const textarea = document.createElement('textarea');
    textarea.readOnly = true;
    textarea.value = pre.textContent;
    textarea.setAttribute('aria-label', options.label || pre.getAttribute('aria-label') || 'Text preview');
    pre.after(textarea);
    const language = options.language || pre.dataset.codeLanguage || 'plaintext';
    const lines = pre.textContent.split('\n').length;
    const handle = await attach(textarea, { minLines: Math.min(lines, 6), maxLines: Number(pre.dataset.codeMaxLines || 18), lineNumbers: language !== 'dagon-log', prose: false, ...options, language, readOnly: true });
    if (!handle) { textarea.remove(); delete pre.dataset.codeAttached; return null; }
    handle.host.classList.add('dw-code-view');
    pre.hidden = true;
    pre.dataset.codeAttached = 'true';
    pre.codeEditor = handle;
    const dispose = handle.dispose;
    handle.dispose = () => { dispose(); textarea.remove(); pre.hidden = false; delete pre.dataset.codeAttached; delete pre.codeEditor; };
    return handle;
  }

  /** Upgrade every textarea and pre marked with data-code-language inside root. */
  function enhance(root = document) {
    return Promise.all([
      ...[...root.querySelectorAll('textarea[data-code-language]:not([data-code-attached])')].map(element => attach(element)),
      ...[...root.querySelectorAll('pre[data-code-language]:not([data-code-attached])')].map(element => view(element)),
    ]);
  }

  function disposeWithin(root) {
    handles.forEach(handle => { if (root.contains(handle.host) || !handle.host.isConnected) handle.dispose(); });
  }

  const languageForFile = name => {
    const lower = String(name || '').toLowerCase();
    if (lower.endsWith('dockerfile')) return 'dockerfile';
    if (lower === 'requirements.txt') return 'pip-requirements';
    return EXTENSIONS[lower.split('.').pop()] || 'plaintext';
  };

  window.DAGonWebCode = { load, attach, view, enhance, disposeWithin, languageForFile, handles, focused: () => [...handles].find(handle => handle.editor.hasTextFocus()) || null };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => enhance());
  else enhance();
})();
