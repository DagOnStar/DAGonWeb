/* DAGonWeb run monitor: live DAGonStar task status, logs, and scratch browser. */
(() => {
  'use strict';

  const layout = document.getElementById('runLayout');
  if (!layout) return;
  const graph = JSON.parse(document.getElementById('runGraph').textContent);
  const specs = JSON.parse(document.getElementById('runSpecs').textContent);
  const workspace = document.getElementById('runWorkspace');
  const svg = document.getElementById('runLinks');
  const inspector = document.getElementById('runInspector');
  const statusBadge = document.getElementById('runStatus');
  const runLog = document.getElementById('runLog');
  const follow = document.getElementById('followLog');
  const resumeForm = document.getElementById('resumeForm');
  const NODE_WIDTH = 180;
  const ACTIVE = new Set(['pending', 'running']);
  const BADGE = { success: 'text-bg-success', failed: 'text-bg-danger', running: 'text-bg-primary', pending: 'text-bg-secondary' };

  let runTasks = {};
  let selected = null;
  let lastLog = '';
  let currentPath = '';

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const taskUrl = (template, id, path) => template.replace('/0/', `/${id}/`) + (path ? `?path=${encodeURIComponent(path)}` : '');

  function duration(ms) {
    const seconds = Math.max(0, Math.round(ms / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  // ---------------------------------------------------------------- graph
  function nodeHeight(name) {
    const node = workspace.querySelector(`.run-node[data-name="${CSS.escape(name)}"]`);
    return node ? node.offsetHeight : 52;
  }

  function draw() {
    workspace.querySelectorAll('.run-node').forEach(node => node.remove());
    const tasks = Object.values(graph.tasks || {});
    tasks.forEach(task => {
      const position = task.dagonweb || {};
      const kind = task.dagonweb?.task_type || task.type;
      const spec = specs[kind] || { icon: '?', label: kind, category: 'shell' };
      const state = runTasks[task.name];
      const status = state?.status || 'pending';
      const node = document.createElement('button');
      node.type = 'button';
      node.className = `run-node cat-${spec.category} ${status}${selected === task.name ? ' selected' : ''}`;
      node.dataset.name = task.name;
      node.style.left = `${position.x ?? 100}px`;
      node.style.top = `${position.y ?? 100}px`;
      node.disabled = !state;
      node.setAttribute('aria-label', `${task.name}: ${status}`);
      node.innerHTML = `<div class="node-head"><span class="type-icon" aria-hidden="true">${escapeHtml(spec.icon)}</span>
        <div class="flex-grow-1 overflow-hidden"><div class="node-title">${escapeHtml(task.name)}</div><div class="node-type">${escapeHtml(spec.label)} · ${escapeHtml(status)}</div></div>
        <span class="status-dot" aria-hidden="true"></span></div>`;
      node.addEventListener('click', () => { selected = task.name; draw(); inspect(runTasks[task.name]); });
      workspace.appendChild(node);
    });
    const width = Math.max(workspace.clientWidth, ...tasks.map(task => (task.dagonweb?.x ?? 100) + NODE_WIDTH + 120));
    const height = Math.max(workspace.clientHeight, ...tasks.map(task => (task.dagonweb?.y ?? 100) + 160));
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    svg.innerHTML = `<defs><marker id="runArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="var(--dw-link)"/></marker>
      <marker id="runArrowExplicit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="var(--dw-link-explicit)"/></marker>
      <marker id="runArrowStream" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="var(--dw-link-stream)"/></marker></defs>`;
    tasks.forEach(target => (target.prevs || []).forEach(sourceName => {
      const source = graph.tasks[sourceName];
      if (!source) return;
      const explicit = (target.explicit_prevs || []).includes(sourceName);
      const config = JSON.stringify(target.dagonweb?.config || {});
      const escapedName = sourceName.replace(/[-]/g, '\\-');
      const stream = !explicit && new RegExp(`workflow://[A-Za-z0-9_-]*/${escapedName}/[^\\s"]+?\\?stream`).test(config);
      const a = { x: (source.dagonweb?.x ?? 100) + NODE_WIDTH, y: (source.dagonweb?.y ?? 100) + nodeHeight(sourceName) / 2 };
      const b = { x: target.dagonweb?.x ?? 100, y: (target.dagonweb?.y ?? 100) + nodeHeight(target.name) / 2 };
      const bend = Math.max(50, Math.abs(b.x - a.x) / 2);
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('class', `link-line${explicit ? ' explicit' : ''}${stream ? ' stream' : ''}`);
      path.setAttribute('d', `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`);
      path.setAttribute('marker-end', `url(#${explicit ? 'runArrowExplicit' : stream ? 'runArrowStream' : 'runArrow'})`);
      svg.appendChild(path);
    }));
  }

  // ---------------------------------------------------------------- previews
  function csvRows(text, delimiter) {
    const rows = [[]];
    let cell = '';
    let quoted = false;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (char === '"') {
        if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted;
      } else if (char === delimiter && !quoted) {
        rows.at(-1).push(cell); cell = '';
      } else if ((char === '\n' || char === '\r') && !quoted) {
        if (char === '\r' && text[i + 1] === '\n') i++;
        rows.at(-1).push(cell); cell = '';
        if (rows.length > 100) break;
        rows.push([]);
      } else cell += char;
    }
    if (cell || rows.at(-1).length) rows.at(-1).push(cell);
    return rows.filter(row => row.length > 1 || row[0] !== '');
  }

  const geoFeatures = value => (value.type === 'FeatureCollection' ? value.features || [] : value.type === 'Feature' ? [value] : [{ type: 'Feature', geometry: value, properties: {} }]);
  function geoPoints(geometry) {
    if (!geometry) return [];
    if (geometry.type === 'Point') return [geometry.coordinates];
    if (geometry.type === 'GeometryCollection') return geometry.geometries.flatMap(geoPoints);
    return (geometry.coordinates || []).flat(Infinity).reduce((points, value, index, all) => (index % 2 === 0 && typeof value === 'number' ? [...points, [value, all[index + 1]]] : points), []);
  }

  // Previews are <pre> elements that static/js/code-editor.js turns into
  // read-only Monaco views with highlighting, folding and search.
  function pre(text, language = 'plaintext') {
    const element = document.createElement('pre');
    element.className = 'file-preview mono small';
    element.dataset.codeLanguage = language;
    element.setAttribute('aria-label', 'File preview');
    element.textContent = text;
    return element;
  }
  const languageFor = name => window.DAGonWebCode?.languageForFile(name) || 'plaintext';

  function renderPreview(target, result, name = '') {
    target.replaceChildren();
    const label = document.createElement('div');
    label.className = 'small text-body-secondary mb-2';
    label.textContent = { json: 'Formatted JSON', geojson: 'GeoJSON map', csv: 'CSV table', netcdf: 'NetCDF metadata', text: 'Text preview' }[result.kind] || 'File preview';
    target.append(label);
    if (result.kind === 'json') {
      try { target.append(pre(JSON.stringify(JSON.parse(result.content), null, 2), 'json')); } catch { target.append(pre(result.content, languageFor(name))); }
    } else if (result.kind === 'netcdf') {
      target.append(pre(JSON.stringify(result.metadata, null, 2), 'json'));
    } else if (result.kind === 'csv') {
      const wrap = document.createElement('div');
      wrap.className = 'table-responsive file-table';
      const table = document.createElement('table');
      table.className = 'table table-sm table-striped';
      csvRows(result.content, result.delimiter).forEach((row, index) => {
        const tr = document.createElement('tr');
        row.forEach(value => { const cell = document.createElement(index ? 'td' : 'th'); cell.textContent = value; tr.append(cell); });
        table.append(tr);
      });
      wrap.append(table);
      target.append(wrap);
    } else if (result.kind === 'geojson') {
      let value;
      try { value = JSON.parse(result.content); } catch { target.append(pre('Invalid GeoJSON')); return; }
      const points = geoFeatures(value).flatMap(feature => geoPoints(feature.geometry));
      const box = points.reduce((b, [x, y]) => [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)], [Infinity, Infinity, -Infinity, -Infinity]);
      const spanX = box[2] - box[0] || 1;
      const spanY = box[3] - box[1] || 1;
      const map = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      map.setAttribute('class', 'geojson-map');
      map.setAttribute('viewBox', '0 0 500 300');
      points.forEach(([x, y]) => {
        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', 20 + (x - box[0]) * 460 / spanX);
        circle.setAttribute('cy', 280 - (y - box[1]) * 260 / spanY);
        circle.setAttribute('r', '4');
        map.append(circle);
      });
      target.append(map);
    } else {
      target.append(pre(result.content, languageFor(name)));
    }
    window.DAGonWebCode?.enhance(target);
  }

  // ---------------------------------------------------------------- inspector
  async function inspect(task, path = '') {
    if (!task) return;
    currentPath = path;
    const response = await fetch(taskUrl(layout.dataset.filesUrl, task.id, path));
    const log = task.log ? `<pre class="file-preview mono small mb-3" style="max-height:10rem" data-code-language="dagon-log" data-code-max-lines="10" aria-label="Task log">${escapeHtml(task.log)}</pre>` : '';
    if (!response.ok) {
      inspector.innerHTML = `<h2 class="h6">${escapeHtml(task.name)} <span class="badge ${BADGE[task.status] || 'text-bg-secondary'}">${escapeHtml(task.status)}</span></h2>${log}<p class="text-body-secondary">Scratch files are not available yet.</p>`;
      return;
    }
    const data = await response.json();
    const breadcrumbs = ['', ...path.split('/').filter(Boolean)];
    inspector.innerHTML = `
      <h2 class="h6">${escapeHtml(task.name)} <span class="badge ${BADGE[task.status] || 'text-bg-secondary'}">${escapeHtml(task.status)}</span></h2>
      ${log}
      <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
        <nav aria-label="Scratch path" class="small mono flex-grow-1">${breadcrumbs.map((part, index) => `<a href="#" data-crumb="${escapeHtml(breadcrumbs.slice(1, index + 1).join('/'))}">${index ? escapeHtml(part) : 'scratch'}</a>`).join(' / ')}</nav>
        ${layout.dataset.archiveUrl ? `<a class="btn btn-sm btn-outline-secondary" href="${escapeHtml(taskUrl(layout.dataset.archiveUrl, task.id))}">Download ZIP</a>` : ''}
      </div>
      <div class="list-group list-group-flush border rounded mb-2" style="max-height:16rem;overflow:auto">
        ${data.files.map(file => `<button type="button" class="list-group-item list-group-item-action d-flex justify-content-between py-1" data-name="${escapeHtml(file.name)}" data-dir="${file.is_dir}">
          <span>${file.is_dir ? '📁' : '📄'} ${escapeHtml(file.name)}</span><span class="small text-body-secondary">${file.is_dir ? '' : formatSize(file.size)}</span></button>`).join('') || '<div class="list-group-item text-body-secondary">No files.</div>'}
      </div>
      <div id="filePreview" aria-live="polite" class="small text-body-secondary">Select a file to preview it.</div>`;
    window.DAGonWebCode?.enhance(inspector);
    inspector.querySelectorAll('[data-crumb]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); inspect(task, link.dataset.crumb); }));
    inspector.querySelectorAll('[data-name]').forEach(button => button.addEventListener('click', async () => {
      const next = path ? `${path}/${button.dataset.name}` : button.dataset.name;
      if (button.dataset.dir === 'true') { inspect(task, next); return; }
      const target = document.getElementById('filePreview');
      target.textContent = 'Loading…';
      const preview = await fetch(taskUrl(layout.dataset.fileUrl, task.id, next));
      const result = await preview.json().catch(() => ({ error: 'Unable to preview file.' }));
      if (preview.ok) renderPreview(target, result, button.dataset.name); else target.textContent = result.error || 'Unable to preview file.';
      const download = document.createElement('a');
      download.className = 'btn btn-sm btn-outline-primary mt-2';
      download.textContent = `Download ${button.dataset.name}`;
      download.href = taskUrl(layout.dataset.downloadUrl, task.id, next);
      target.append(download);
    }));
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  }

  // ---------------------------------------------------------------- polling
  // The live log becomes a read-only Monaco view (log highlighting, search with
  // Ctrl/⌘+F); the <pre> stays as the fallback when Monaco is unavailable.
  let logView = null;
  window.DAGonWebCode?.view(runLog, { language: 'dagon-log', label: 'Run log', minLines: 12, maxLines: 24 }).then(view => {
    logView = view;
    if (view) { view.setValue(lastLog || 'Waiting for workflow activity…'); if (follow.checked) view.revealEnd(); }
  });

  function renderLog(text) {
    if (text === lastLog) return;
    lastLog = text;
    if (logView) {
      logView.setValue(text || 'Waiting for workflow activity…');
      if (follow.checked) logView.revealEnd();
      return;
    }
    runLog.innerHTML = escapeHtml(text || 'Waiting for workflow activity…')
      .replace(/^(.*(?:failed|error).*)$/gim, '<span class="log-error">$1</span>')
      .replace(/^(.*(?:completed with status: success|completed\.).*)$/gim, '<span class="log-ok">$1</span>');
    if (follow.checked) runLog.scrollTop = runLog.scrollHeight;
  }

  function renderSummary(data) {
    const tasks = data.tasks;
    const total = Math.max(tasks.length, Object.keys(graph.tasks || {}).length);
    const count = status => tasks.filter(task => task.status === status).length;
    const done = count('success');
    const failed = count('failed');
    const running = count('running');
    const percent = value => (total ? (100 * value) / total : 0);
    document.getElementById('runProgressSuccess').style.width = `${percent(done)}%`;
    document.getElementById('runProgressFailed').style.width = `${percent(failed)}%`;
    document.getElementById('runProgressRunning').style.width = `${percent(running)}%`;
    document.querySelector('.run-progress').setAttribute('aria-valuenow', String(Math.round(percent(done + failed))));
    const counts = `${done}/${total} completed · ${running} running · ${failed} failed`;
    const countsEl = document.getElementById('runCounts');
    if (countsEl.textContent !== counts) countsEl.textContent = counts;
    const end = ACTIVE.has(data.status) ? Date.now() : Date.parse(data.updated_at);
    document.getElementById('runElapsed').textContent = `${ACTIVE.has(data.status) ? 'running for' : 'took'} ${duration(end - Date.parse(data.created_at))}`;
  }

  async function poll() {
    try {
      const response = await fetch(layout.dataset.statusUrl, { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      statusBadge.textContent = data.status;
      statusBadge.className = `badge align-middle ${BADGE[data.status] || 'text-bg-secondary'}`;
      if (resumeForm) resumeForm.hidden = !data.can_resume;
      renderLog(data.log);
      runTasks = Object.fromEntries(data.tasks.map(task => [task.name, task]));
      renderSummary(data);
      renderInteractions(data.interactions || []);
      renderResults(data.results || []);
      draw();
      // Refresh the selected task's log and file list while it runs, unless the user is browsing.
      if (selected && runTasks[selected] && !currentPath && !document.getElementById('filePreview')?.querySelector('pre, table, svg')) {
        inspect(runTasks[selected]);
      }
      if (ACTIVE.has(data.status)) setTimeout(poll, 1000);
      else if (!selected) {
        const firstFailed = data.tasks.find(task => task.status === 'failed');
        if (firstFailed) { selected = firstFailed.name; draw(); inspect(firstFailed); }
      }
    } catch {
      setTimeout(poll, 3000);
    }
  }

  // ------------------------------------------------------- interactive tasks
  const interactionsBox = document.getElementById('runInteractions');
  const resultsBox = document.getElementById('runResults');
  let interactionKey = '';
  let resultKey = '';

  function fieldControl(field) {
    const id = `form_${field.name}`;
    const required = field.required ? 'required' : '';
    const value = escapeHtml(field.default ?? '');
    const label = `<label class="form-label small fw-semibold" for="${id}">${escapeHtml(field.label)}${field.required ? ' *' : ''}</label>`;
    const help = field.help ? `<div class="form-text">${escapeHtml(field.help)}</div>` : '';
    if (field.type === 'boolean') {
      return `<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" role="switch" id="${id}" name="${escapeHtml(field.name)}" value="true" ${field.default ? 'checked' : ''}><label class="form-check-label" for="${id}">${escapeHtml(field.label)}</label>${help}</div>`;
    }
    if (field.type === 'select') {
      const options = (field.options || []).map(option => `<option ${String(option) === String(field.default) ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('');
      return `<div class="mb-2">${label}<select class="form-select form-select-sm" id="${id}" name="${escapeHtml(field.name)}" ${required}>${options}</select>${help}</div>`;
    }
    if (field.type === 'textarea') {
      return `<div class="mb-2">${label}<textarea class="form-control form-control-sm" id="${id}" name="${escapeHtml(field.name)}" rows="3" data-code-language="plaintext" ${required}>${value}</textarea>${help}</div>`;
    }
    const type = { number: 'number', integer: 'number', date: 'date', email: 'email' }[field.type] || 'text';
    const step = field.type === 'number' ? 'step="any"' : field.type === 'integer' ? 'step="1"' : '';
    return `<div class="mb-2">${label}<input class="form-control form-control-sm" type="${type}" ${step} id="${id}" name="${escapeHtml(field.name)}" value="${value}" ${required}>${help}</div>`;
  }

  function renderInteractions(items) {
    const key = JSON.stringify(items.map(item => [item.task, item.running]));
    if (key === interactionKey) return;
    interactionKey = key;
    interactionsBox.hidden = !items.length;
    interactionsBox.innerHTML = items.map(item => `
      <form class="card interaction-card mb-3" data-task="${escapeHtml(item.task)}" data-kind="${escapeHtml(item.kind)}" ${item.kind === 'upload' ? 'enctype="multipart/form-data"' : ''}>
        <div class="card-header d-flex justify-content-between align-items-center"><span><strong>${escapeHtml(item.task)}</strong> needs your ${item.kind === 'upload' ? 'files' : 'input'}</span>
          <span class="badge ${item.running ? 'text-bg-primary' : 'text-bg-secondary'}">${item.running ? 'waiting' : 'starts soon'}</span></div>
        <div class="card-body">
          ${item.description ? `<p class="small mb-2">${escapeHtml(item.description)}</p>` : ''}
          ${item.kind === 'upload'
            ? `<label class="form-label small" for="upload_${escapeHtml(item.task)}">File${item.multiple ? 's' : ''} (max ${item.max_mb} MB${item.accept ? `, ${escapeHtml(item.accept)}` : ''})</label>
               <input class="form-control" type="file" id="upload_${escapeHtml(item.task)}" name="files" ${item.multiple ? 'multiple' : ''} accept="${escapeHtml(item.accept)}" required>
               ${item.multiple ? '' : `<div class="form-text">Saved as <code>outputs/${escapeHtml(item.save_as)}</code>.</div>`}`
            : (item.fields || []).map(fieldControl).join('')}
          <div class="d-flex align-items-center gap-2 mt-2"><button class="btn btn-primary btn-sm">${item.kind === 'upload' ? 'Upload and continue' : 'Submit and continue'}</button><span class="small text-danger" role="alert" data-error></span></div>
        </div>
      </form>`).join('');
    window.DAGonWebCode?.enhance(interactionsBox);
    interactionsBox.querySelectorAll('form').forEach(form => form.addEventListener('submit', async event => {
      event.preventDefault();
      const error = form.querySelector('[data-error]');
      const button = form.querySelector('button');
      error.textContent = '';
      button.disabled = true;
      const body = new FormData(form);
      if (form.dataset.kind === 'form') form.querySelectorAll('input[type=checkbox]').forEach(box => { if (!box.checked) body.set(box.name, 'false'); });
      try {
        const response = await fetch(layout.dataset.interactUrl.replace('__task__', encodeURIComponent(form.dataset.task)), { method: 'POST', body, headers: { 'X-CSRFToken': layout.dataset.csrf, Accept: 'application/json' } });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
        form.remove();
        interactionKey = '';
        if (!interactionsBox.querySelector('form')) interactionsBox.hidden = true;
      } catch (exception) {
        error.textContent = exception.message;
        button.disabled = false;
      }
    }));
  }

  function chartColors(count) {
    const styles = getComputedStyle(document.documentElement);
    const base = ['--dw-cat-shell', '--dw-cat-python_ai', '--dw-cat-containers', '--dw-cat-services', '--dw-cat-interactive', '--dw-cat-data', '--dw-link-explicit'].map(name => styles.getPropertyValue(name).trim());
    return Array.from({ length: count }, (_, index) => base[index % base.length]);
  }

  function drawChart(canvas, spec) {
    if (!window.Chart) return false;
    const styles = getComputedStyle(document.documentElement);
    const text = styles.getPropertyValue('--bs-body-color').trim();
    const grid = styles.getPropertyValue('--bs-border-color').trim();
    const pie = spec.type === 'pie';
    const colors = chartColors(pie ? spec.labels.length : spec.series.length);
    const datasets = spec.series.map((series, index) => ({
      label: series.name,
      data: spec.type === 'scatter' ? series.values.map((value, i) => ({ x: Number(spec.labels[i]), y: value })) : series.values,
      backgroundColor: pie ? colors : colors[index],
      borderColor: pie ? styles.getPropertyValue('--bs-body-bg').trim() : colors[index],
      borderWidth: pie ? 2 : 2,
      pointRadius: spec.type === 'line' ? 2 : 3,
    }));
    new window.Chart(canvas, {
      type: spec.type === 'scatter' ? 'scatter' : spec.type,
      data: { labels: spec.labels, datasets: pie ? datasets.slice(0, 1) : datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: text } }, title: { display: Boolean(spec.title), text: spec.title, color: text } },
        scales: pie ? {} : { x: { ticks: { color: text }, grid: { color: grid } }, y: { ticks: { color: text }, grid: { color: grid } } },
      },
    });
    return true;
  }

  function renderResults(items) {
    const key = JSON.stringify(items.map(item => item.task));
    if (key === resultKey) return;
    resultKey = key;
    resultsBox.hidden = !items.length;
    const body = resultsBox.querySelector('.card-body');
    body.innerHTML = '';
    items.forEach(item => {
      const section = document.createElement('section');
      section.className = 'mb-4';
      const title = item.data?.title || item.task;
      if (item.kind === 'download') {
        section.innerHTML = `<h3 class="h6">${escapeHtml(item.task)}</h3><a class="btn btn-outline-primary" href="${escapeHtml(item.url)}">⇩ Download ${escapeHtml(item.name)}</a> <span class="small text-body-secondary">${formatSize(item.size)}</span>`;
      } else if (item.kind === 'table') {
        const columns = item.data.columns || [];
        const rows = item.data.rows || [];
        section.innerHTML = `<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-1"><h3 class="h6 mb-0">${escapeHtml(title)}</h3><span class="small text-body-secondary">${rows.length} row(s) · <a href="${escapeHtml(item.csv_url)}">CSV<span class="visually-hidden"> of ${escapeHtml(title)}</span></a></span></div>
          <div class="result-table border rounded" tabindex="0" role="region" aria-label="${escapeHtml(title)}"><table class="table table-sm table-striped mb-0 small"><caption class="visually-hidden">${escapeHtml(title)}</caption><thead><tr>${columns.map(column => `<th scope="col">${escapeHtml(column)}</th>`).join('')}</tr></thead>
          <tbody>${rows.slice(0, 500).map(row => `<tr>${row.map(cell => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      } else if (item.kind === 'chart') {
        const spec = item.data || {};
        const series = spec.series || [];
        const labels = spec.labels || [];
        const summary = `${spec.type || 'chart'} chart of ${series.map(entry => entry.name).join(', ') || 'values'} by ${spec.x || 'label'}, ${labels.length} point(s)`;
        // Text alternative (WCAG 1.1.1): the same values as a data table.
        const table = `<details class="chart-data mt-2"><summary>Data table for “${escapeHtml(title)}”</summary><div class="result-table border rounded mt-1"><table class="table table-sm mb-0 small">
          <caption class="visually-hidden">${escapeHtml(title)}</caption>
          <thead><tr><th scope="col">${escapeHtml(spec.x || 'label')}</th>${series.map(entry => `<th scope="col">${escapeHtml(entry.name)}</th>`).join('')}</tr></thead>
          <tbody>${labels.slice(0, 500).map((label, index) => `<tr><th scope="row">${escapeHtml(label)}</th>${series.map(entry => `<td>${escapeHtml(entry.values?.[index])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>`;
        section.innerHTML = `<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-1"><h3 class="h6 mb-0">${escapeHtml(title)}</h3><a class="small" href="${escapeHtml(item.svg_url)}">SVG<span class="visually-hidden"> of ${escapeHtml(title)}</span></a></div><div class="result-chart"><canvas role="img" aria-label="${escapeHtml(`${title}: ${summary}. The data table below lists the values.`)}"></canvas></div>${table}`;
        body.append(section);
        if (!drawChart(section.querySelector('canvas'), item.data)) section.querySelector('.result-chart').textContent = 'Chart library unavailable; download the SVG instead.';
        return;
      }
      body.append(section);
    });
  }

  // ---------------------------------------------------------------- resizer
  const detailsResizer = document.getElementById('runDetailsResizer');
  function setDetailsWidth(width) {
    const maximum = Math.max(280, layout.clientWidth - 320);
    const next = Math.round(Math.min(Math.max(width, 280), maximum));
    layout.style.gridTemplateColumns = `minmax(320px,1fr) ${next}px`;
    if (window.DAGonWebConsent?.allows('preferences')) { try { localStorage.setItem('dagonweb.run-details-width', String(next)); } catch { /* ignore */ } }
  }
  if (window.DAGonWebConsent?.allows('preferences')) { try { const saved = Number(localStorage.getItem('dagonweb.run-details-width')); if (saved) setDetailsWidth(saved); } catch { /* ignore */ } }
  detailsResizer.addEventListener('pointerdown', event => {
    event.preventDefault();
    detailsResizer.setPointerCapture(event.pointerId);
    const move = pointer => setDetailsWidth(layout.getBoundingClientRect().right - pointer.clientX);
    const end = () => { detailsResizer.removeEventListener('pointermove', move); draw(); };
    detailsResizer.addEventListener('pointermove', move);
    detailsResizer.addEventListener('pointerup', end, { once: true });
    detailsResizer.addEventListener('pointercancel', end, { once: true });
  });
  detailsResizer.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const current = document.getElementById('runDetailsColumn').getBoundingClientRect().width;
    setDetailsWidth(current + (event.key === 'ArrowLeft' ? 24 : -24));
  });
  follow.addEventListener('change', () => { if (follow.checked) { runLog.scrollTop = runLog.scrollHeight; logView?.revealEnd(); } });
  window.addEventListener('resize', draw);

  draw();
  poll();
})();
