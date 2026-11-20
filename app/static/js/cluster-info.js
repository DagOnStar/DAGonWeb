/* Account page: storage, quota and Slurm queue of the user's own cluster account.
   Read on demand from /account/cluster/info; every value is inserted with
   textContent, never as HTML. */
(() => {
  const root = document.getElementById('clusterInfo');
  if (!root) return;
  const status = document.getElementById('clusterInfoStatus');
  const body = document.getElementById('clusterInfoBody');
  const storage = document.getElementById('clusterStorage');
  const quota = document.getElementById('clusterQuota');
  const queueWrap = document.getElementById('clusterQueueWrap');
  const queue = document.getElementById('clusterQueue');
  const note = document.getElementById('clusterQueueNote');
  const refresh = document.getElementById('clusterInfoRefresh');

  const size = bytes => {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toFixed(unit && value < 10 ? 1 : 0)} ${units[unit]}`;
  };

  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };

  const term = (label, value) => {
    storage.append(element('dt', label, 'col-sm-4 fw-normal text-body-secondary'), element('dd', value, 'col-sm-8 mb-1'));
  };

  const render = data => {
    storage.replaceChildren();
    const fs = data.filesystem || {};
    const mine = data.dagonweb || {};
    term('DAGonWeb folder', data.base || '—');
    term('Used by DAGonWeb runs', `${size(mine.bytes)}${mine.truncated ? ' or more' : ''} in ${mine.runs || 0} run folder${mine.runs === 1 ? '' : 's'}`);
    if (typeof fs.total === 'number') term('File system', `${size(fs.free)} free of ${size(fs.total)}`);
    quota.hidden = !data.quota;
    if (data.quota) quota.textContent = data.quota.text || (data.quota.ok ? 'No quota report.' : 'The quota command failed.');
    queue.replaceChildren();
    const jobs = data.queue || [];
    queueWrap.hidden = !jobs.length;
    for (const job of jobs) {
      const row = document.createElement('tr');
      row.append(element('td', job.id));
      const name = element('td');
      if (job.run_url) {
        const link = element('a', job.run_label || job.name);
        link.href = job.run_url;
        name.append(link);
      } else {
        name.textContent = job.name;
      }
      row.append(name, element('td', job.state), element('td', job.elapsed), element('td', job.limit), element('td', job.partition), element('td', job.reason));
      queue.append(row);
    }
    if (!data.slurm) note.textContent = 'Slurm is not available on the login node.';
    else if (data.queue_error) note.textContent = `squeue: ${data.queue_error}`;
    else if (!jobs.length) note.textContent = 'You have no jobs in the Slurm queue.';
    else note.textContent = `${jobs.length} job${jobs.length === 1 ? '' : 's'} in the queue. Jobs started by DAGonWeb link to their run.`;
    body.hidden = false;
  };

  const load = async () => {
    refresh.disabled = true;
    status.hidden = false;
    status.textContent = 'Reading from the cluster…';
    try {
      const response = await fetch(root.dataset.url, {headers: {Accept: 'application/json'}, credentials: 'same-origin'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      render(data);
      status.textContent = `Updated at ${new Date().toLocaleTimeString()}.`;
    } catch (error) {
      status.textContent = error.message || 'Cannot read the cluster information.';
    } finally {
      refresh.disabled = false;
    }
  };

  refresh.addEventListener('click', load);
  load();
})();
