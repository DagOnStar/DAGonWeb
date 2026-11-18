/* DAGonWeb workflow assistant: chat with an LLM agent that designs, implements
   and dry-runs the workflow as a draft, then apply the draft to the canvas. */
(() => {
  'use strict';

  const panel = document.getElementById('assistantPanel');
  if (!panel) return;
  const log = document.getElementById('assistantLog');
  const form = document.getElementById('assistantForm');
  const input = document.getElementById('assistantInput');
  const sendButton = document.getElementById('assistantSend');
  const draftBox = document.getElementById('assistantDraft');
  const csrf = panel.dataset.csrf;
  const storageKey = `dagonweb.assistant.${panel.dataset.workflowId}`;
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let messages = [];
  let draft = null;
  try { messages = JSON.parse(sessionStorage.getItem(storageKey) || '[]'); } catch { messages = []; }
  const remember = () => { try { sessionStorage.setItem(storageKey, JSON.stringify(messages.slice(-20))); } catch { /* private mode */ } };

  function bubble(role, text, trace) {
    const item = document.createElement('div');
    item.className = `assistant-msg ${role}`;
    item.textContent = text;
    if (trace && trace.length) {
      const details = document.createElement('div');
      details.className = 'assistant-trace';
      details.innerHTML = trace.map(step => `${step.ok ? '✓' : '✗'} <code>${escapeHtml(step.tool)}</code> ${escapeHtml(step.summary)}`).join('<br>');
      item.append(details);
    }
    log.append(item);
    log.scrollTop = log.scrollHeight;
    return item;
  }

  function renderHistory() {
    log.innerHTML = '';
    if (!messages.length) {
      bubble('assistant', panel.dataset.enabled === '1'
        ? 'Describe the workflow you need, or ask me to change, explain or test the current one. I work on a draft; you apply it to the canvas.'
        : 'The assistant is not configured yet. An administrator can enable it under Admin → LLM connector. You can still dry-run the workflow with “Check workflow”.');
    }
    messages.forEach(message => bubble(message.role, message.content));
  }

  function summarizeDraft(document) {
    const tasks = Object.values(document.tasks || {});
    return tasks.map(task => `<li><code>${escapeHtml(task.name)}</code> <span class="text-body-secondary">${escapeHtml(task.type)}</span>${(task.explicit_prevs || []).length ? ` · after ${task.explicit_prevs.map(escapeHtml).join(', ')}` : ''}</li>`).join('');
  }

  function showDraft(document, check) {
    draft = document;
    if (!document) { draftBox.hidden = true; return; }
    const problems = (check?.problems || []).map(problem => `<li class="text-danger">${escapeHtml(problem)}</li>`).join('');
    const warnings = (check?.warnings || []).map(warning => `<li class="text-warning-emphasis">${escapeHtml(warning)}</li>`).join('');
    draftBox.innerHTML = `
      <div class="d-flex justify-content-between align-items-center mb-1"><strong>Draft</strong>
        <span class="badge ${check?.ok ? 'text-bg-success' : 'text-bg-warning'}">${check?.ok ? 'dry run passed' : 'needs attention'}</span></div>
      <ul class="mb-1 ps-3">${summarizeDraft(document)}</ul>
      ${problems || warnings ? `<ul class="mb-1 ps-3 small">${problems}${warnings}</ul>` : ''}
      <div class="d-flex gap-2 mt-2">
        <button type="button" class="btn btn-sm btn-primary" data-apply ${window.DAGonWebEditor?.editable ? '' : 'disabled'}>Apply to canvas</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" data-discard>Discard</button>
      </div>`;
    draftBox.hidden = false;
    draftBox.querySelector('[data-apply]').addEventListener('click', async () => {
      const ok = await window.DAGonWebEditor.applyDocument(draft);
      bubble('assistant', ok ? 'Draft applied and saved. Press ▶ Run to execute it.' : 'The draft could not be saved; see the status next to the title.');
      if (ok) showDraft(null);
    });
    draftBox.querySelector('[data-discard]').addEventListener('click', () => showDraft(null));
  }

  async function post(url, body) {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, Accept: 'application/json' }, body: JSON.stringify(body) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  async function send(text) {
    const content = text.trim();
    if (!content) return;
    messages.push({ role: 'user', content });
    remember();
    bubble('user', content);
    const pending = bubble('assistant', 'Thinking… (designing, checking and dry-running the draft)');
    sendButton.disabled = true;
    input.value = '';
    try {
      const result = await post(panel.dataset.chatUrl, { messages, draft: draft || window.DAGonWebEditor?.getDocument() });
      pending.remove();
      messages.push({ role: 'assistant', content: result.reply });
      remember();
      bubble('assistant', result.reply, result.trace);
      if (result.changed) showDraft(result.draft, result.check);
    } catch (error) {
      pending.remove();
      messages.pop();
      remember();
      bubble('error', error.message);
      input.value = content;
    } finally {
      sendButton.disabled = false;
      input.focus();
    }
  }

  // Monaco (static/js/code-editor.js): Markdown highlighting, task-name and
  // workflow:/// completion from the open workflow, Ctrl/⌘+Enter to send.
  window.DAGonWebCode?.attach(input, {
    language: 'markdown', label: 'Message the assistant', minLines: 3, maxLines: 10,
    onSubmit: () => send(input.value),
    context: () => window.DAGonWebEditor?.codeContext?.(),
    suggestions: ['Explain', 'Add a task that', 'Run after', 'Fix the last run', 'Add a table and a chart of'],
  });
  form.addEventListener('submit', event => { event.preventDefault(); send(input.value); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(input.value); }
  });
  panel.querySelectorAll('[data-prompt]').forEach(chip => chip.addEventListener('click', () => send(chip.dataset.prompt)));
  document.getElementById('assistantCheck').addEventListener('click', async () => {
    try {
      const result = await post(panel.dataset.validateUrl, { draft: draft || window.DAGonWebEditor?.getDocument() });
      const lines = [result.ok ? `Dry run passed. ${result.dagonstar || ''}` : 'The dry run found problems:'];
      (result.problems || []).forEach(problem => lines.push(`• ${problem}`));
      if (result.error) lines.push(`• ${result.error}`);
      (result.warnings || []).forEach(warning => lines.push(`⚠ ${warning}`));
      bubble(result.ok ? 'assistant' : 'error', lines.join('\n'));
    } catch (error) { bubble('error', error.message); }
  });
  document.getElementById('assistantClear').addEventListener('click', () => { messages = []; remember(); showDraft(null); renderHistory(); });

  renderHistory();
  if (new URLSearchParams(window.location.search).has('assistant')) {
    window.bootstrap?.Offcanvas.getOrCreateInstance(panel).show();
    input.focus();
  }
})();
