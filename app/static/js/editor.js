/* DAGonWeb visual workflow editor.
 *
 * Plain browser JavaScript, no build step. The task-type registry comes from
 * the server (app/task_specs.py) as JSON, so palette entries and inspector
 * forms always match what the executor hands to DAGonStar.
 *
 * Data links are never stored on their own: they are derived from the
 * workflow:/// references inside each task configuration, exactly like
 * DAGonStar does. Explicit (ordering-only) dependencies are stored per task
 * as DAGonStar `explicit_prevs`.
 */
(() => {
  'use strict';

  const root = document.getElementById('editor');
  if (!root) return;
  const workspace = document.getElementById('workspace');
  const canvas = document.getElementById('canvas');
  const svg = document.getElementById('links');
  const inspector = document.getElementById('inspectorBody');
  const statusEl = document.getElementById('saveStatus');
  const emptyHint = document.getElementById('canvasEmpty');
  const editable = root.dataset.editable === '1';

  const SPEC_GROUPS = JSON.parse(document.getElementById('taskSpecs').textContent);
  const SPECS = Object.fromEntries(SPEC_GROUPS.flatMap(group => group.types).map(spec => [spec.type, spec]));
  const NAME_RE = /^[A-Za-z0-9_-]{1,80}$/;
  const INPUT_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
  const PATH_RE = /^[A-Za-z0-9_.-]+(\/[A-Za-z0-9_.-]+)*$/;
  // Mirrors app.task_specs.WORKFLOW_REFERENCE.
  const REFERENCE_RE = /workflow:\/\/([A-Za-z0-9_-]{0,80})\/([A-Za-z0-9_-]{1,80})\/([^\s;|&<>()]+)/g;
  const NODE_WIDTH = 180;
  const COMMAND_TYPES = new Set(['batch', 'slurm', 'checkpoint', 'cloud', 'docker', 'kubernetes', 'apptainer', 'nomad']);
  // Where a new data link stores its reference, per target task type.
  const INPUT_FIELD = { llm: 'input_files', web: null };

  let graph = { name: '', id: 0, tasks: [], links: [] };
  let selected = null;
  let drag = null;
  let linkDrag = null;
  let zoom = 1;
  let saveTimer = null;
  let saving = Promise.resolve();
  let aceEditors = [];

  // ------------------------------------------------------------------ utils
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clone = value => JSON.parse(JSON.stringify(value ?? {}));
  const findTask = name => graph.tasks.find(task => task.name === name);
  const specOf = task => SPECS[task.task_type] || { label: task.task_type, icon: '?', category: 'shell', fields: [] };

  function setStatus(text, state = '') {
    statusEl.textContent = text;
    statusEl.className = `editor-status ${state}`;
  }

  function references(value, input = 'input') {
    if (typeof value === 'string') {
      return [...value.matchAll(REFERENCE_RE)].map(match => ({ workflow: match[1], source: match[2], path: match[3], input }));
    }
    if (Array.isArray(value)) return value.flatMap(item => references(item, input));
    if (value && typeof value === 'object') {
      return Object.entries(value).flatMap(([key, item]) => references(item, /^[A-Za-z0-9_-]+$/.test(key) ? key : input));
    }
    return [];
  }

  function taskReferences(task) {
    const config = { ...(task.config || {}) };
    if (task.task_type === 'native') delete config.source;
    return references(config).filter(ref => !ref.workflow || ref.workflow === graph.name);
  }

  function deriveLinks() {
    const links = [];
    const seen = new Set();
    graph.tasks.forEach(target => {
      taskReferences(target).forEach(ref => {
        const key = `${ref.source}|${target.name}`;
        if (ref.source === target.name || seen.has(key)) return;
        seen.add(key);
        links.push({ kind: 'data', source: ref.source, target: target.name, path: ref.path, input: ref.input, missing: !findTask(ref.source) });
      });
      (target.explicit_prevs || []).forEach(source => {
        const key = `${source}|${target.name}`;
        if (seen.has(key)) return;
        seen.add(key);
        links.push({ kind: 'explicit', source, target: target.name, missing: !findTask(source) });
      });
    });
    graph.links = links;
  }

  /** Files a task is known to produce, used for reference suggestions. */
  function producedFiles(task) {
    const config = task.config || {};
    const files = new Set();
    const addValues = (mapping, prefix = '') => Object.values(mapping || {}).forEach(value => {
      if (typeof value === 'string' && PATH_RE.test(value)) files.add(value.startsWith('outputs/') || !prefix ? value : `${prefix}${value}`);
    });
    if (task.task_type === 'native' || task.task_type === 'faas') addValues(config.outputs, 'outputs/');
    if (['iot', 'agentic', 'web'].includes(task.task_type)) addValues(config.outputs);
    if (task.task_type === 'llm' && config.output_file) files.add(config.output_file);
    // DAGonWeb interactive kinds always write these files.
    const fixed = {
      upload: config.multiple ? [] : [`outputs/${config.save_as || 'data.csv'}`],
      form: ['outputs/form.json', 'outputs/form.env'],
      table: ['outputs/table.json', 'outputs/table.csv', 'outputs/table.html'],
      chart: ['outputs/chart.json', 'outputs/chart.svg'],
      download: [`outputs/${config.archive || 'results.zip'}`],
    }[task.task_type] || [];
    fixed.forEach(file => files.add(file));
    if (COMMAND_TYPES.has(task.task_type)) {
      for (const match of String(config.command || '').matchAll(/>{1,2}\s*([A-Za-z0-9_.\/-]+)/g)) {
        if (PATH_RE.test(match[1])) files.add(match[1]);
      }
    }
    graph.links.filter(link => link.source === task.name && link.path).forEach(link => files.add(link.path));
    return [...files];
  }

  // ------------------------------------------------------------------ load/save
  async function load() {
    const response = await fetch(root.dataset.graphUrl, { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      setStatus('Unable to load the workflow', 'error');
      return;
    }
    fromDocument(await response.json());
    setStatus(editable ? 'All changes saved' : 'Read only', editable ? 'saved' : '');
    render();
    showEmptyInspector();
    if (graph.tasks.length) requestAnimationFrame(() => fit(false));
  }

  function fromDocument(document) {
    graph = {
      name: document.name,
      id: document.id,
      host: document.host || 'localhost',
      pre_stage: document.pre_stage,
      dagonweb: document.dagonweb,
      tasks: Object.values(document.tasks || {}).map(task => ({
        name: task.name,
        task_type: task.dagonweb?.task_type || task.type,
        x: task.dagonweb?.x ?? 100,
        y: task.dagonweb?.y ?? 100,
        config: task.dagonweb?.config ?? {},
        explicit_prevs: [...(task.explicit_prevs || [])],
      })),
      links: [],
    };
    deriveLinks();
  }

  function toDocument() {
    const tasks = {};
    graph.tasks.forEach(task => {
      tasks[task.name] = {
        name: task.name,
        type: task.task_type,
        command: '',
        explicit_prevs: (task.explicit_prevs || []).filter(name => findTask(name)),
        dagonweb: { x: Math.round(task.x), y: Math.round(task.y), config: task.config },
      };
    });
    const document = { tasks, name: graph.name, id: graph.id, host: graph.host };
    if (graph.pre_stage) document.pre_stage = graph.pre_stage;
    if (graph.dagonweb) document.dagonweb = graph.dagonweb;
    return document;
  }

  function scheduleSave(delay = 400) {
    if (!editable) return;
    clearTimeout(saveTimer);
    setStatus('Unsaved changes…', 'saving');
    saveTimer = setTimeout(save, delay);
  }

  function save() {
    clearTimeout(saveTimer);
    if (!editable) return Promise.resolve();
    const body = JSON.stringify(toDocument());
    saving = saving.then(async () => {
      setStatus('Saving…', 'saving');
      try {
        const response = await fetch(root.dataset.saveUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.csrf },
          body,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || `Save failed (HTTP ${response.status})`);
        setStatus(`Saved ${new Date().toLocaleTimeString()}`, 'saved');
        return true;
      } catch (error) {
        setStatus(`Not saved: ${error.message}`, 'error');
        return false;
      }
    });
    return saving;
  }

  // ------------------------------------------------------------------ rendering
  function render() {
    canvas.querySelectorAll('.task-node').forEach(node => node.remove());
    const missing = new Set(graph.links.filter(link => link.missing).map(link => link.target));
    graph.tasks.forEach(task => canvas.appendChild(renderNode(task, missing.has(task.name))));
    emptyHint.hidden = graph.tasks.length > 0;
    resizeCanvas();
    drawLinks();
  }

  function renderNode(task, hasMissing) {
    const spec = specOf(task);
    const node = document.createElement('div');
    node.className = `task-node cat-${spec.category}${selected === task.name ? ' selected' : ''}`;
    node.dataset.name = task.name;
    node.tabIndex = 0;
    node.setAttribute('role', 'button');
    node.setAttribute('aria-label', `${task.name}, ${spec.label} task`);
    node.style.left = `${task.x}px`;
    node.style.top = `${task.y}px`;
    node.innerHTML = `
      <button class="connector connector-input" type="button" aria-label="Connect into ${escapeHtml(task.name)}" tabindex="-1"></button>
      <div class="node-head"><span class="type-icon" aria-hidden="true">${escapeHtml(spec.icon)}</span>
        <div class="min-w-0"><div class="node-title">${escapeHtml(task.name)}</div><div class="node-type">${escapeHtml(spec.label)}</div></div></div>
      ${hasMissing ? '<div class="node-warning">⚠ references a missing task</div>' : ''}
      <button class="connector connector-output" type="button" aria-label="Connect from ${escapeHtml(task.name)}" tabindex="-1"></button>`;

    node.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('.connector')) return;
      select(task.name);
      if (!editable) return;
      const point = canvasPoint(event);
      drag = { name: task.name, dx: point.x - task.x, dy: point.y - task.y, moved: false };
    });
    node.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(task.name); }
    });
    node.querySelector('.connector-output').addEventListener('pointerdown', event => {
      if (!editable) return;
      event.preventDefault();
      event.stopPropagation();
      const anchor = outputAnchor(task);
      linkDrag = { source: task.name, x: anchor.x, y: anchor.y, mx: anchor.x, my: anchor.y };
      workspace.classList.add('workspace-linking');
      drawLinks();
    });
    return node;
  }

  function nodeHeight(name) {
    const node = canvas.querySelector(`.task-node[data-name="${CSS.escape(name)}"]`);
    return node ? node.offsetHeight : 56;
  }
  const outputAnchor = task => ({ x: task.x + NODE_WIDTH, y: task.y + nodeHeight(task.name) / 2 });
  const inputAnchor = task => ({ x: task.x, y: task.y + nodeHeight(task.name) / 2 });

  function curve(a, b) {
    const bend = Math.max(50, Math.abs(b.x - a.x) / 2);
    return `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`;
  }

  function resizeCanvas() {
    const width = Math.max(workspace.clientWidth / zoom, ...graph.tasks.map(task => task.x + NODE_WIDTH + 240));
    const height = Math.max(workspace.clientHeight / zoom, ...graph.tasks.map(task => task.y + 200));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.style.transform = `scale(${zoom})`;
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    document.getElementById('zoomLabel').textContent = `${Math.round(zoom * 100)}%`;
  }

  function drawLinks() {
    const ns = 'http://www.w3.org/2000/svg';
    svg.innerHTML = `<defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="var(--dw-link)"/></marker>
      <marker id="arrowExplicit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="var(--dw-link-explicit)"/></marker></defs>`;
    graph.links.forEach(link => {
      const a = findTask(link.source);
      const b = findTask(link.target);
      if (!a || !b) return;
      const d = curve(outputAnchor(a), inputAnchor(b));
      const group = document.createElementNS(ns, 'g');
      const hit = document.createElementNS(ns, 'path');
      hit.setAttribute('class', 'link-hit');
      hit.setAttribute('d', d);
      const title = document.createElementNS(ns, 'title');
      title.textContent = link.kind === 'explicit'
        ? `${link.source} → ${link.target} (explicit dependency). Click to remove.`
        : `${link.target}.${link.input} ← workflow:///${link.source}/${link.path}. Click to remove.`;
      hit.appendChild(title);
      if (editable) hit.addEventListener('click', event => { event.stopPropagation(); removeLink(link); });
      const path = document.createElementNS(ns, 'path');
      path.setAttribute('class', `link-line ${link.kind === 'explicit' ? 'explicit' : ''} ${selected && (link.source === selected || link.target === selected) ? 'highlight' : ''}`);
      path.setAttribute('d', d);
      path.setAttribute('marker-end', `url(#${link.kind === 'explicit' ? 'arrowExplicit' : 'arrow'})`);
      group.append(hit, path);
      svg.appendChild(group);
    });
    if (linkDrag) {
      const draft = document.createElementNS(ns, 'path');
      draft.setAttribute('class', 'link-line draft');
      draft.setAttribute('d', curve({ x: linkDrag.x, y: linkDrag.y }, { x: linkDrag.mx, y: linkDrag.my }));
      svg.appendChild(draft);
    }
  }

  // ------------------------------------------------------------------ geometry & zoom
  function canvasPoint(event) {
    const rect = canvas.getBoundingClientRect();
    return { x: (event.clientX - rect.left) / zoom, y: (event.clientY - rect.top) / zoom };
  }

  function setZoom(value) {
    zoom = Math.min(2, Math.max(0.3, Math.round(value * 100) / 100));
    resizeCanvas();
    drawLinks();
  }

  function fit(allowGrow = true) {
    if (!graph.tasks.length) return setZoom(1);
    const maxX = Math.max(...graph.tasks.map(task => task.x + NODE_WIDTH)) + 60;
    const maxY = Math.max(...graph.tasks.map(task => task.y + 80)) + 60;
    const scale = Math.min(workspace.clientWidth / maxX, workspace.clientHeight / maxY);
    setZoom(allowGrow ? Math.min(scale, 1.25) : Math.min(scale, 1));
  }

  // ------------------------------------------------------------------ graph edits
  function uniqueName(base) {
    const stem = (base || 'task').replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 70) || 'task';
    let name = stem;
    let index = 2;
    while (findTask(name)) name = `${stem}_${index++}`;
    return name;
  }

  function addTask(type, position) {
    const spec = SPECS[type];
    if (!spec || !editable) return;
    const name = uniqueName(spec.default_name);
    const config = clone(spec.default_config);
    if (type === 'native' && config.callable === 'workflow_functions:run') config.callable = `workflow_functions.${name}:run`;
    const center = position || {
      x: (workspace.scrollLeft + workspace.clientWidth / 2) / zoom - NODE_WIDTH / 2,
      y: (workspace.scrollTop + workspace.clientHeight / 2) / zoom - 30,
    };
    graph.tasks.push({ name, task_type: type, x: Math.max(10, Math.round(center.x)), y: Math.max(10, Math.round(center.y)), config, explicit_prevs: [] });
    deriveLinks();
    select(name);
    scheduleSave(0);
  }

  function referenceField(task) {
    if (task.task_type in INPUT_FIELD) return INPUT_FIELD[task.task_type];
    return 'inputs';
  }

  function addDataLink(source, target, path, input) {
    const field = referenceField(target);
    const reference = `workflow:///${source.name}/${path}`;
    const container = target.config[field] && typeof target.config[field] === 'object' && !Array.isArray(target.config[field]) ? target.config[field] : {};
    target.config = { ...target.config, [field]: { ...container, [input]: reference } };
  }

  function removeLink(link) {
    const target = findTask(link.target);
    if (!target) return;
    if (link.kind === 'explicit') {
      if (!confirm(`Remove the explicit dependency ${link.source} → ${link.target}?`)) return;
      target.explicit_prevs = (target.explicit_prevs || []).filter(name => name !== link.source);
    } else {
      const prefix = `workflow:///${link.source}/`;
      const field = referenceField(target);
      const mapping = field ? target.config[field] : null;
      const keys = mapping && typeof mapping === 'object' ? Object.keys(mapping).filter(key => typeof mapping[key] === 'string' && mapping[key].startsWith(prefix)) : [];
      const elsewhere = taskReferences({ ...target, config: { ...target.config, [field]: Object.fromEntries(Object.entries(mapping || {}).filter(([key]) => !keys.includes(key))) } }).some(ref => ref.source === link.source);
      if (!keys.length) {
        alert(`The reference to ${link.source} is part of ${target.name}'s configuration (for example its command). Edit it in the Inspector to remove the link.`);
        select(target.name);
        return;
      }
      if (!confirm(`Remove input${keys.length > 1 ? 's' : ''} ${keys.join(', ')} of ${target.name}?`)) return;
      const next = { ...mapping };
      keys.forEach(key => delete next[key]);
      target.config = { ...target.config, [field]: next };
      if (elsewhere) alert(`${target.name} still references ${link.source} elsewhere in its configuration, so the link remains.`);
    }
    deriveLinks();
    render();
    if (selected) showInspector(findTask(selected));
    scheduleSave(0);
  }

  function deleteTask(name) {
    const task = findTask(name);
    if (!task || !editable) return;
    const dependants = graph.links.filter(link => link.source === name).map(link => link.target);
    const message = dependants.length
      ? `Delete ${name}? ${[...new Set(dependants)].join(', ')} depend${dependants.length === 1 ? 's' : ''} on it; their named inputs and explicit dependencies on ${name} are removed too.`
      : `Delete ${name}?`;
    if (!confirm(message)) return;
    graph.tasks = graph.tasks.filter(item => item !== task);
    const prefix = `workflow:///${name}/`;
    graph.tasks.forEach(other => {
      other.explicit_prevs = (other.explicit_prevs || []).filter(source => source !== name);
      const field = referenceField(other);
      const mapping = field ? other.config[field] : null;
      if (mapping && typeof mapping === 'object' && !Array.isArray(mapping)) {
        other.config = { ...other.config, [field]: Object.fromEntries(Object.entries(mapping).filter(([, value]) => !(typeof value === 'string' && value.startsWith(prefix)))) };
      }
    });
    selected = null;
    deriveLinks();
    render();
    showEmptyInspector();
    scheduleSave(0);
  }

  function duplicateTask(name) {
    const task = findTask(name);
    if (!task || !editable) return;
    const copy = { ...clone(task), name: uniqueName(`${task.name}_copy`), x: task.x + 40, y: task.y + 40 };
    graph.tasks.push(copy);
    deriveLinks();
    select(copy.name);
    scheduleSave(0);
  }

  function renameReferences(value, oldName, newName) {
    if (typeof value === 'string') {
      const escaped = oldName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const workflow = (graph.name || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      return value
        .replace(new RegExp(`workflow:///${escaped}(?=/)`, 'g'), `workflow:///${newName}`)
        .replace(new RegExp(`workflow://${workflow}/${escaped}(?=/)`, 'g'), `workflow://${graph.name}/${newName}`);
    }
    if (Array.isArray(value)) return value.map(item => renameReferences(item, oldName, newName));
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, renameReferences(item, oldName, newName)]));
    return value;
  }

  function autoLayout() {
    const names = graph.tasks.map(task => task.name);
    const incoming = Object.fromEntries(names.map(name => [name, 0]));
    const nexts = Object.fromEntries(names.map(name => [name, []]));
    graph.links.forEach(link => {
      if (nexts[link.source] && link.target in incoming) { nexts[link.source].push(link.target); incoming[link.target]++; }
    });
    const level = Object.fromEntries(names.map(name => [name, 0]));
    const queue = names.filter(name => !incoming[name]);
    while (queue.length) {
      const name = queue.shift();
      nexts[name].forEach(target => {
        level[target] = Math.max(level[target], level[name] + 1);
        if (!--incoming[target]) queue.push(target);
      });
    }
    const columns = {};
    names.forEach(name => (columns[level[name]] ||= []).push(name));
    Object.entries(columns).forEach(([column, members]) => {
      members.sort().forEach((name, index) => {
        const task = findTask(name);
        task.x = 60 + Number(column) * 260;
        task.y = 60 + index * 120 + (members.length === 1 ? 60 : 0);
      });
    });
    render();
    fit();
    scheduleSave(0);
  }

  // ------------------------------------------------------------------ selection & inspector
  function select(name) {
    if (selected === name && inspector.querySelector('#applyInspector')) return;
    if (selected && selected !== name) {
      const previous = findTask(selected);
      if (previous && inspectorDirty() && !applyInspector(previous, { quiet: true })) return;
    }
    selected = name;
    render();
    const task = findTask(name);
    if (task) showInspector(task);
  }

  function clearSelection() {
    if (selected) {
      const previous = findTask(selected);
      if (previous && inspectorDirty() && !applyInspector(previous, { quiet: true })) return;
    }
    selected = null;
    render();
    showEmptyInspector();
  }

  function showEmptyInspector() {
    destroyAce();
    const counts = graph.tasks.length
      ? `<p class="small text-body-secondary">${graph.tasks.length} task${graph.tasks.length === 1 ? '' : 's'}, ${graph.links.filter(link => link.kind === 'data').length} data link(s), ${graph.links.filter(link => link.kind === 'explicit').length} explicit dependenc${graph.links.filter(link => link.kind === 'explicit').length === 1 ? 'y' : 'ies'}.</p>`
      : '';
    inspector.innerHTML = `<p class="text-body-secondary">Select a task to edit it.</p>${counts}
      <div class="small text-body-secondary"><span class="d-inline-block border-top border-2 me-1" style="width:24px;border-color:var(--dw-link)!important"></span>data link (<code>workflow:///</code>)<br>
      <span class="d-inline-block border-top border-2 me-1" style="width:24px;border-style:dashed!important;border-color:var(--dw-link-explicit)!important"></span>explicit dependency (<code>explicit_prevs</code>)</div>`;
  }

  const disabledAttr = () => (editable ? '' : 'disabled');

  function fieldValue(field, config) {
    const value = config[field.key];
    switch (field.kind) {
      case 'mapping': return Object.entries(value || {}).map(([key, item]) => `${key} = ${typeof item === 'string' ? item : JSON.stringify(item)}`).join('\n');
      case 'json': return value === undefined || value === null || value === '' ? '' : JSON.stringify(value, null, 2);
      case 'list': return Array.isArray(value) ? value.join('\n') : (value || '');
      default: return value ?? field.default ?? '';
    }
  }

  function renderField(field, config) {
    const id = `cfg_${field.key}`;
    const help = field.help ? `<div class="form-text">${field.help}</div>` : '';
    const value = fieldValue(field, config);
    const label = `<label class="form-label small fw-semibold mb-1" for="${id}">${escapeHtml(field.label)}</label>`;
    if (field.kind === 'checkbox') {
      const checked = config[field.key] ?? field.default ?? false;
      return `<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" role="switch" id="${id}" ${checked ? 'checked' : ''} ${disabledAttr()}><label class="form-check-label small" for="${id}">${escapeHtml(field.label)}</label>${help}</div>`;
    }
    if (field.kind === 'tristate') {
      const current = config[field.key] === true ? 'true' : config[field.key] === false ? 'false' : '';
      const options = [['', 'Workflow default'], ['true', 'On'], ['false', 'Off']].map(([v, text]) => `<option value="${v}" ${v === current ? 'selected' : ''}>${text}</option>`).join('');
      return `<div class="mb-2">${label}<select class="form-select form-select-sm" id="${id}" ${disabledAttr()}>${options}</select>${help}</div>`;
    }
    if (field.kind === 'select') {
      const options = [...new Set([...(field.options || []), ...(value && !(field.options || []).includes(value) ? [value] : [])])];
      return `<div class="mb-2">${label}<select class="form-select form-select-sm" id="${id}" ${disabledAttr()}>${options.map(option => `<option ${option === value ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}</select>${help}</div>`;
    }
    if (field.kind === 'code') {
      return `<div class="mb-2">${label}<textarea class="form-control form-control-sm mono" id="${id}" rows="8" spellcheck="false" data-mode="${field.mode || 'sh'}" ${disabledAttr()}>${escapeHtml(value)}</textarea>${help}</div>`;
    }
    if (['mapping', 'json', 'list', 'textarea'].includes(field.kind)) {
      const rows = Math.min(12, Math.max(field.kind === 'json' ? 4 : 3, String(value).split('\n').length + 1));
      return `<div class="mb-2">${label}<textarea class="form-control form-control-sm mono" id="${id}" rows="${rows}" spellcheck="false" placeholder="${escapeHtml(field.placeholder || (field.kind === 'mapping' ? 'name = value' : ''))}" ${disabledAttr()}>${escapeHtml(value)}</textarea>${help}</div>`;
    }
    const type = field.kind === 'integer' || field.kind === 'number' ? 'number' : field.kind === 'url' ? 'url' : 'text';
    const step = field.kind === 'number' ? 'any' : '1';
    return `<div class="mb-2">${label}<input class="form-control form-control-sm ${type === 'text' ? 'mono' : ''}" id="${id}" type="${type}" ${type === 'number' ? `step="${step}" min="0"` : ''} value="${escapeHtml(value)}" placeholder="${escapeHtml(field.placeholder || '')}" ${disabledAttr()}>${help}</div>`;
  }

  function parseMapping(text, label) {
    const result = {};
    text.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#')).forEach(line => {
      const index = line.indexOf('=');
      if (index < 1) throw new Error(`${label}: use one "name = value" per line`);
      result[line.slice(0, index).trim()] = line.slice(index + 1).trim();
    });
    return result;
  }

  function readField(field, element) {
    const raw = element.type === 'checkbox' ? element.checked : element.value;
    switch (field.kind) {
      case 'checkbox': return raw;
      case 'tristate': return raw === '' ? undefined : raw === 'true';
      case 'integer': {
        if (String(raw).trim() === '') return undefined;
        const value = Number(raw);
        if (!Number.isInteger(value) || value < 0) throw new Error(`${field.label} must be a non-negative integer`);
        return value;
      }
      case 'number': {
        if (String(raw).trim() === '') return undefined;
        const value = Number(raw);
        if (!Number.isFinite(value) || value < 0) throw new Error(`${field.label} must be a non-negative number`);
        return value;
      }
      case 'mapping': return parseMapping(raw, field.label);
      case 'list': return raw.split(/\n|,/).map(item => item.trim()).filter(Boolean);
      case 'json': {
        if (!raw.trim()) return undefined;
        try { return JSON.parse(raw); } catch (error) { throw new Error(`${field.label} must be valid JSON (${error.message})`); }
      }
      default: return raw;
    }
  }

  function showInspector(task) {
    destroyAce();
    const spec = specOf(task);
    const config = task.config || {};
    const basic = spec.fields.filter(field => !field.advanced);
    const advanced = spec.fields.filter(field => field.advanced);
    const incoming = graph.links.filter(link => link.target === task.name);
    const outgoing = graph.links.filter(link => link.source === task.name);
    const others = graph.tasks.filter(other => other.name !== task.name);
    const producers = others.flatMap(other => producedFiles(other).map(path => `workflow:///${other.name}/${path}`));
    const linkItem = (link, otherName) => `<li class="small">${link.kind === 'explicit' ? '<span class="badge text-bg-warning">after</span>' : '<span class="badge text-bg-secondary">data</span>'} <code>${escapeHtml(otherName)}</code>${link.kind === 'data' ? ` <span class="text-body-secondary mono">${escapeHtml(link.path)}</span>` : ''}${link.missing ? ' <span class="text-danger">missing task</span>' : ''}</li>`;

    inspector.innerHTML = `
      <div class="d-flex align-items-center gap-2 mb-2 cat-${spec.category}"><span class="type-icon" aria-hidden="true">${escapeHtml(spec.icon)}</span>
        <div><div class="fw-semibold">${escapeHtml(spec.label)} task</div><div class="small text-body-secondary">${escapeHtml(spec.description)}</div></div></div>
      <div id="inspectorErrors" class="alert alert-danger py-2 small" role="alert" hidden></div>
      <div class="mb-2"><label class="form-label small fw-semibold mb-1" for="iName">Task name</label>
        <input id="iName" class="form-control form-control-sm mono" value="${escapeHtml(task.name)}" pattern="[A-Za-z0-9_-]{1,80}" maxlength="80" ${disabledAttr()}></div>
      <div class="inspector-section">Configuration</div>
      ${basic.map(field => renderField(field, config)).join('')}
      ${advanced.length ? `<details class="mb-2"><summary class="small fw-semibold">Advanced options</summary><div class="pt-2">${advanced.map(field => renderField(field, config)).join('')}</div></details>` : ''}
      ${producers.length && editable ? `<div class="mb-2"><div class="small fw-semibold mb-1">Insert a producer reference</div><div class="d-flex flex-wrap gap-1">${producers.slice(0, 24).map(ref => `<button type="button" class="btn btn-sm btn-outline-secondary mono reference-chip py-0" data-reference="${escapeHtml(ref)}" title="Insert at the cursor of the focused field">${escapeHtml(ref.replace('workflow:///', ''))}</button>`).join('')}</div></div>` : ''}
      <div class="inspector-section">Dependencies</div>
      <div class="mb-2"><label class="form-label small fw-semibold mb-1" for="iExplicit">Run after (explicit dependencies)</label>
        <select id="iExplicit" class="form-select form-select-sm" multiple size="${Math.min(5, Math.max(2, others.length))}" ${disabledAttr()} aria-describedby="iExplicitHelp">
          ${others.map(other => `<option value="${escapeHtml(other.name)}" ${(task.explicit_prevs || []).includes(other.name) ? 'selected' : ''}>${escapeHtml(other.name)}</option>`).join('')}
        </select><div id="iExplicitHelp" class="form-text">Ordering without data staging, saved as DAGonStar <code>explicit_prevs</code>.</div></div>
      <div class="row small g-2 mb-2"><div class="col-6"><div class="fw-semibold">Inputs from</div><ul class="list-unstyled mb-0">${incoming.map(link => linkItem(link, link.source)).join('') || '<li class="text-body-secondary">none</li>'}</ul></div>
        <div class="col-6"><div class="fw-semibold">Feeds</div><ul class="list-unstyled mb-0">${outgoing.map(link => linkItem(link, link.target)).join('') || '<li class="text-body-secondary">none</li>'}</ul></div></div>
      <details class="mb-3"><summary class="small fw-semibold">Raw configuration JSON</summary>
        <textarea id="iConfig" class="form-control form-control-sm mono mt-2" rows="10" spellcheck="false" ${disabledAttr()}>${escapeHtml(JSON.stringify(config, null, 2))}</textarea>
        <div class="form-text">When this panel is open, Apply saves the JSON as written.</div></details>
      <div class="d-flex flex-wrap gap-2">
        <button id="applyInspector" class="btn btn-primary btn-sm" type="button" ${disabledAttr()}>Apply</button>
        <button id="duplicateTask" class="btn btn-outline-secondary btn-sm" type="button" ${disabledAttr()}>Duplicate</button>
        <button id="deleteTask" class="btn btn-outline-danger btn-sm ms-auto" type="button" ${disabledAttr()}>Delete</button>
      </div>`;

    inspector.dataset.snapshot = '';
    inspector.querySelector('#applyInspector').addEventListener('click', () => applyInspector(task));
    inspector.querySelector('#deleteTask').addEventListener('click', () => deleteTask(task.name));
    inspector.querySelector('#duplicateTask').addEventListener('click', () => duplicateTask(task.name));
    inspector.querySelectorAll('.reference-chip').forEach(chip => chip.addEventListener('mousedown', event => event.preventDefault()));
    inspector.querySelectorAll('.reference-chip').forEach(chip => chip.addEventListener('click', () => insertReference(chip.dataset.reference)));
    inspector.addEventListener('focusin', rememberFocus);
    upgradeCodeFields();
    inspector.dataset.snapshot = inspectorSnapshot();
  }

  let lastFocused = null;
  function rememberFocus(event) {
    if (event.target.matches('textarea, input[type="text"], input:not([type])')) lastFocused = event.target;
  }

  function insertReference(reference) {
    const focusedAce = aceEditors.find(item => item.editor.isFocused() || item.editor === lastFocused);
    if (focusedAce) { focusedAce.editor.insert(reference); focusedAce.editor.focus(); return; }
    const target = lastFocused && inspector.contains(lastFocused) ? lastFocused : inspector.querySelector('#cfg_command, #cfg_inputs');
    if (!target) return;
    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? target.value.length;
    target.setRangeText(reference, start, end, 'end');
    target.focus();
  }

  function upgradeCodeFields() {
    if (!window.ace) return;
    const dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    inspector.querySelectorAll('textarea[data-mode]').forEach(textarea => {
      const host = document.createElement('div');
      host.className = `code-editor ${textarea.dataset.mode}`;
      textarea.after(host);
      textarea.hidden = true;
      const editor = window.ace.edit(host);
      editor.setTheme(dark ? 'ace/theme/tomorrow_night' : 'ace/theme/github');
      editor.session.setMode(textarea.dataset.mode === 'python' ? 'ace/mode/python' : 'ace/mode/sh');
      editor.setValue(textarea.value, -1);
      editor.setOptions({ fontSize: '13px', showPrintMargin: false, wrap: true, tabSize: textarea.dataset.mode === 'python' ? 4 : 2, useSoftTabs: true, readOnly: !editable });
      editor.session.on('change', () => { textarea.value = editor.getValue(); });
      editor.on('focus', () => { lastFocused = editor; });
      editor.commands.addCommand({ name: 'apply', bindKey: { win: 'Ctrl-Enter', mac: 'Command-Enter' }, exec: () => applyInspector(findTask(selected)) });
      aceEditors.push({ editor, textarea });
    });
  }

  function destroyAce() {
    aceEditors.forEach(item => item.editor.destroy());
    aceEditors = [];
    lastFocused = null;
  }

  function inspectorSnapshot() {
    return [...inspector.querySelectorAll('input, textarea, select')].map(element => (
      element.type === 'checkbox' ? element.checked : element.multiple ? [...element.selectedOptions].map(option => option.value).join(',') : element.value
    )).join('\u0000');
  }
  const inspectorDirty = () => editable && inspector.querySelector('#applyInspector') && inspector.dataset.snapshot !== inspectorSnapshot();

  function showErrors(message) {
    const box = inspector.querySelector('#inspectorErrors');
    if (!box) return alert(message);
    box.textContent = message;
    box.hidden = !message;
    if (message) box.scrollIntoView({ block: 'nearest' });
  }

  /** Read the inspector into ``task``; returns false (and shows why) on errors. */
  function applyInspector(task, { quiet = false } = {}) {
    if (!task || !editable) return true;
    try {
      const spec = specOf(task);
      const name = inspector.querySelector('#iName').value.trim();
      if (!NAME_RE.test(name)) throw new Error('Task names may contain only letters, numbers, underscores, and hyphens (max 80).');
      if (graph.tasks.some(other => other !== task && other.name === name)) throw new Error(`A task named ${name} already exists.`);
      let config;
      const raw = inspector.querySelector('details[open] #iConfig');
      if (raw) {
        try { config = JSON.parse(raw.value); } catch (error) { throw new Error(`Raw configuration must be valid JSON (${error.message})`); }
        if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('Raw configuration must be a JSON object.');
      } else {
        config = { ...task.config };
        delete config.time_limit;
        spec.fields.forEach(field => {
          const element = inspector.querySelector(`#cfg_${CSS.escape(field.key)}`);
          if (!element) return;
          const value = readField(field, element);
          if (value === undefined) delete config[field.key];
          else config[field.key] = value;
        });
        validateConfig(task, config, name);
      }
      const explicit = [...inspector.querySelector('#iExplicit').selectedOptions].map(option => option.value);
      const oldName = task.name;
      task.config = config;
      task.explicit_prevs = explicit;
      if (oldName !== name) {
        graph.tasks.forEach(other => {
          other.config = renameReferences(other.config, oldName, name);
          other.explicit_prevs = (other.explicit_prevs || []).map(source => (source === oldName ? name : source));
        });
        task.name = name;
        if (task.task_type === 'native' && typeof config.callable === 'string') {
          config.callable = config.callable.replace(`workflow_functions.${oldName}:`, `workflow_functions.${name}:`);
        }
        selected = name;
      }
      deriveLinks();
      render();
      showInspector(task);
      scheduleSave(0);
      return true;
    } catch (error) {
      showErrors(error.message);
      if (!quiet) inspector.querySelector('#inspectorErrors')?.focus?.();
      return false;
    }
  }

  function validateConfig(task, config, name) {
    const type = task.task_type;
    const mappingNames = config.inputs && typeof config.inputs === 'object' && !Array.isArray(config.inputs) ? Object.keys(config.inputs) : [];
    if (COMMAND_TYPES.has(type) && mappingNames.some(key => !INPUT_RE.test(key))) {
      throw new Error('Named inputs must be shell identifiers (letters, digits, underscores; not starting with a digit).');
    }
    if (['docker', 'kubernetes', 'apptainer', 'nomad'].includes(type) && !String(config.image || '').trim()) throw new Error('Set the container image.');
    if (type === 'native') {
      const source = String(config.source || '');
      if (!config.callable && source.trim()) {
        const match = source.match(/^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/m);
        if (match) config.callable = `workflow_functions.${name}:${match[1]}`;
      }
      if (!/^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$/.test(config.callable || '')) throw new Error('Set Callable to an importable module:function name.');
      if (source.length > 250000) throw new Error('Native source must be 250 KB or smaller.');
      Object.entries(config.outputs || {}).forEach(([key, path]) => {
        if (!INPUT_RE.test(key) || !PATH_RE.test(String(path))) throw new Error('Native outputs need a parameter name and a safe relative path.');
      });
    }
    if (type === 'llm') {
      if (!config.provider) throw new Error('Set the LLM provider name.');
      if (!config.prompt || typeof config.prompt !== 'object' || !Array.isArray(config.prompt.messages)) throw new Error('The LLM request must be a JSON object with a messages array.');
      if (!config.output_file || !PATH_RE.test(config.output_file)) throw new Error('Response file must be a safe relative path.');
    }
    if (type === 'faas' && (!config.provider || !config.function)) throw new Error('FaaS tasks need a provider and a function.');
    if (type === 'agentic' && (!Array.isArray(config.requirements) || !config.requirements.length)) throw new Error('Agentic tasks need at least one requirement.');
    if (type === 'web' && !/^https?:\/\//.test(config.url || '')) throw new Error('Web tasks need an http(s) URL.');
  }

  // ------------------------------------------------------------------ connect dialog
  const connectModalEl = document.getElementById('connectModal');
  const connectModal = window.bootstrap ? new window.bootstrap.Modal(connectModalEl) : null;
  let pendingConnection = null;

  function openConnectDialog(sourceName, targetName) {
    const source = findTask(sourceName);
    const target = findTask(targetName);
    if (!source || !target || source === target) return;
    if (graph.links.some(link => link.source === sourceName && link.target === targetName)) {
      setStatus(`${sourceName} already feeds ${targetName}`, '');
      return;
    }
    if (wouldCycle(sourceName, targetName)) {
      setStatus(`Not connected: ${targetName} → … → ${sourceName} already exists, so the graph would have a cycle`, 'error');
      return;
    }
    pendingConnection = { source, target };
    const supportsData = referenceField(target) !== null;
    document.getElementById('connectSummary').innerHTML = `<code>${escapeHtml(sourceName)}</code> → <code>${escapeHtml(targetName)}</code>`;
    document.getElementById('connectData').disabled = !supportsData;
    document.getElementById(supportsData ? 'connectData' : 'connectExplicit').checked = true;
    const files = producedFiles(source);
    document.getElementById('connectPaths').innerHTML = files.map(file => `<option value="${escapeHtml(file)}">`).join('');
    document.getElementById('connectPath').value = files[0] || 'output.txt';
    const field = referenceField(target);
    const existing = Object.keys((field && target.config[field]) || {});
    let input = sourceName.replace(/[^A-Za-z0-9_]/g, '_').replace(/^(\d)/, '_$1') || 'input';
    let index = 2;
    const base = input;
    while (existing.includes(input)) input = `${base}_${index++}`;
    document.getElementById('connectInput').value = input;
    document.getElementById('connectHint').innerHTML = COMMAND_TYPES.has(target.task_type)
      ? `Stored in <code>inputs</code>; use <code>$${escapeHtml(input)}</code> in the command for the staged path.`
      : `Stored in <code>${escapeHtml(field || 'inputs')}</code> of ${escapeHtml(targetName)}.`;
    toggleConnectFields();
    if (connectModal) connectModal.show();
  }

  function toggleConnectFields() {
    const data = document.getElementById('connectData').checked;
    document.getElementById('connectDataFields').hidden = !data;
    document.querySelectorAll('#connectDataFields input').forEach(input => { input.required = data; });
  }
  document.querySelectorAll('input[name="connectKind"]').forEach(radio => radio.addEventListener('change', toggleConnectFields));
  document.getElementById('connectInput').addEventListener('input', event => {
    const target = pendingConnection?.target;
    if (target && COMMAND_TYPES.has(target.task_type)) document.getElementById('connectHint').innerHTML = `Stored in <code>inputs</code>; use <code>$${escapeHtml(event.target.value)}</code> in the command for the staged path.`;
  });

  document.getElementById('connectForm').addEventListener('submit', event => {
    event.preventDefault();
    if (!pendingConnection) return;
    const { source, target } = pendingConnection;
    if (document.getElementById('connectExplicit').checked) {
      target.explicit_prevs = [...new Set([...(target.explicit_prevs || []), source.name])];
    } else {
      const path = document.getElementById('connectPath').value.trim();
      const input = document.getElementById('connectInput').value.trim();
      if (!PATH_RE.test(path) || !INPUT_RE.test(input)) return;
      addDataLink(source, target, path, input);
    }
    pendingConnection = null;
    connectModal?.hide();
    deriveLinks();
    if (selected === target.name) { render(); showInspector(target); } else select(target.name);
    scheduleSave(0);
  });

  function wouldCycle(source, target) {
    const stack = [target];
    const seen = new Set();
    while (stack.length) {
      const name = stack.pop();
      if (name === source) return true;
      if (seen.has(name)) continue;
      seen.add(name);
      graph.links.filter(link => link.source === name).forEach(link => stack.push(link.target));
    }
    return false;
  }

  // ------------------------------------------------------------------ global events
  document.querySelectorAll('#palette [data-type]').forEach(button => {
    button.addEventListener('click', () => addTask(button.dataset.type));
    button.addEventListener('dragstart', event => { event.dataTransfer.setData('application/x-dagonweb-type', button.dataset.type); event.dataTransfer.effectAllowed = 'copy'; });
  });
  workspace.addEventListener('dragover', event => { if (editable && event.dataTransfer.types.includes('application/x-dagonweb-type')) event.preventDefault(); });
  workspace.addEventListener('drop', event => {
    const type = event.dataTransfer.getData('application/x-dagonweb-type');
    if (!type) return;
    event.preventDefault();
    const point = canvasPoint(event);
    addTask(type, { x: point.x - NODE_WIDTH / 2, y: point.y - 25 });
  });

  document.getElementById('paletteFilter').addEventListener('input', event => {
    const query = event.target.value.trim().toLowerCase();
    document.querySelectorAll('#palette .palette-item').forEach(item => { item.hidden = Boolean(query) && !item.textContent.toLowerCase().includes(query); });
    document.querySelectorAll('#palette [data-palette-group]').forEach(group => {
      let next = group.nextElementSibling;
      let visible = false;
      while (next && next.classList.contains('palette-item')) { visible ||= !next.hidden; next = next.nextElementSibling; }
      group.hidden = !visible;
    });
  });

  document.addEventListener('pointermove', event => {
    if (linkDrag) {
      const point = canvasPoint(event);
      linkDrag.mx = point.x;
      linkDrag.my = point.y;
      drawLinks();
      return;
    }
    if (!drag) return;
    const task = findTask(drag.name);
    if (!task) return;
    const point = canvasPoint(event);
    const x = Math.max(0, Math.round((point.x - drag.dx) / 10) * 10);
    const y = Math.max(0, Math.round((point.y - drag.dy) / 10) * 10);
    if (x === task.x && y === task.y) return;
    task.x = x;
    task.y = y;
    drag.moved = true;
    const node = canvas.querySelector(`.task-node[data-name="${CSS.escape(task.name)}"]`);
    if (node) { node.style.left = `${x}px`; node.style.top = `${y}px`; }
    drawLinks();
  });

  document.addEventListener('pointerup', event => {
    if (linkDrag) {
      const node = document.elementFromPoint(event.clientX, event.clientY)?.closest('.task-node');
      const source = linkDrag.source;
      linkDrag = null;
      workspace.classList.remove('workspace-linking');
      drawLinks();
      if (node && node.dataset.name !== source) openConnectDialog(source, node.dataset.name);
      return;
    }
    if (drag) {
      const moved = drag.moved;
      drag = null;
      if (moved) { resizeCanvas(); scheduleSave(); }
    }
  });

  workspace.addEventListener('pointerdown', event => {
    if (event.target === workspace || event.target === canvas || event.target === svg) clearSelection();
  });

  document.addEventListener('keydown', event => {
    const typing = event.target.closest('input, textarea, select, .ace_editor, [contenteditable]');
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === 's') {
      event.preventDefault();
      const task = selected && findTask(selected);
      if (task && inspectorDirty() && !applyInspector(task)) return;
      save();
      return;
    }
    if (mod && event.key === 'Enter' && selected) { event.preventDefault(); applyInspector(findTask(selected)); return; }
    if (typing || document.querySelector('.modal.show')) return;
    if (!selected) return;
    const task = findTask(selected);
    if (!task) return;
    if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); deleteTask(task.name); }
    else if (event.key === 'Escape') clearSelection();
    else if (mod && event.key.toLowerCase() === 'd') { event.preventDefault(); duplicateTask(task.name); }
    else if (editable && event.key.startsWith('Arrow')) {
      event.preventDefault();
      const step = event.shiftKey ? 50 : 10;
      task.x = Math.max(0, task.x + (event.key === 'ArrowRight' ? step : event.key === 'ArrowLeft' ? -step : 0));
      task.y = Math.max(0, task.y + (event.key === 'ArrowDown' ? step : event.key === 'ArrowUp' ? -step : 0));
      render();
      canvas.querySelector(`.task-node[data-name="${CSS.escape(task.name)}"]`)?.focus();
      scheduleSave();
    }
  });

  document.getElementById('autoLayout').addEventListener('click', autoLayout);
  document.getElementById('zoomIn').addEventListener('click', () => setZoom(zoom + 0.1));
  document.getElementById('zoomOut').addEventListener('click', () => setZoom(zoom - 0.1));
  document.getElementById('zoomReset').addEventListener('click', () => setZoom(1));
  document.getElementById('zoomFit').addEventListener('click', () => fit());
  workspace.addEventListener('wheel', event => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    setZoom(zoom - Math.sign(event.deltaY) * 0.1);
  }, { passive: false });
  window.addEventListener('resize', () => { resizeCanvas(); drawLinks(); });

  // Save pending inspector edits before running, so the run uses them.
  document.getElementById('runForm').addEventListener('submit', async event => {
    event.preventDefault();
    const task = selected && findTask(selected);
    if (task && inspectorDirty() && !applyInspector(task)) return;
    if (editable && !(await save())) return;
    if (!graph.tasks.length) { setStatus('Add at least one task before running', 'error'); return; }
    event.target.submit();
  });
  window.addEventListener('beforeunload', event => {
    if (editable && (inspectorDirty() || statusEl.classList.contains('saving') || statusEl.classList.contains('error'))) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  // ------------------------------------------------------------------ inspector width
  const resizer = document.getElementById('inspectorResizer');
  const toggleFull = document.getElementById('toggleInspectorFull');
  function setExpanded(expanded) {
    root.classList.toggle('inspector-expanded', expanded);
    toggleFull.textContent = expanded ? 'Standard' : 'Expand';
    toggleFull.title = expanded ? 'Restore standard Inspector width' : 'Expand Inspector';
    toggleFull.setAttribute('aria-pressed', String(expanded));
    aceEditors.forEach(item => item.editor.resize());
    if (!expanded) { resizeCanvas(); drawLinks(); }
  }
  function setInspectorWidth(width) {
    setExpanded(false);
    const bounds = root.getBoundingClientRect();
    const max = Math.min(640, Math.max(280, bounds.width - 240 - 320 - 24));
    const value = Math.max(280, Math.min(max, width));
    root.style.setProperty('--inspector-width', `${value}px`);
    resizer.setAttribute('aria-valuemax', String(Math.round(max)));
    resizer.setAttribute('aria-valuenow', String(Math.round(value)));
    try { localStorage.setItem('dagonweb.inspector-width', String(Math.round(value))); } catch { /* ignore */ }
    aceEditors.forEach(item => item.editor.resize());
  }
  toggleFull.addEventListener('click', () => setExpanded(!root.classList.contains('inspector-expanded')));
  resizer.addEventListener('pointerdown', event => {
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    const move = moveEvent => setInspectorWidth(root.getBoundingClientRect().right - moveEvent.clientX);
    const up = () => { resizer.removeEventListener('pointermove', move); resizer.removeEventListener('pointerup', up); resizeCanvas(); drawLinks(); };
    resizer.addEventListener('pointermove', move);
    resizer.addEventListener('pointerup', up);
  });
  resizer.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const current = parseFloat(getComputedStyle(root).getPropertyValue('--inspector-width')) || 360;
    setInspectorWidth(current + (event.key === 'ArrowLeft' ? 20 : -20));
  });
  try {
    const saved = Number(localStorage.getItem('dagonweb.inspector-width'));
    if (saved) setInspectorWidth(saved);
  } catch { /* ignore */ }

  // Hooks for the workflow assistant (assistant.js).
  window.DAGonWebEditor = {
    editable,
    getDocument: () => toDocument(),
    async applyDocument(document) {
      if (!editable) return false;
      fromDocument({ ...document, name: graph.name, id: graph.id, dagonweb: graph.dagonweb });
      render();
      showEmptyInspector();
      requestAnimationFrame(() => fit(false));
      return save();
    },
  };

  showEmptyInspector();
  load();
})();
