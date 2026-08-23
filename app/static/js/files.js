// Output Files Explorer Module

let currentFilesRunId = null;

function updateFilesRunDropdown() {
  const select = document.getElementById('files-run-select');
  if (!select) return;

  const runs = AppState.allRuns || [];
  let html = '<option value="">-- Select run to inspect output files --</option>';

  runs.forEach(r => {
    const selected = r.id === currentFilesRunId ? 'selected' : '';
    const label = `[${r.account_username}] ${r.title} (${r.status})`;
    html += `<option value="${esc(r.id)}" ${selected}>${esc(label)}</option>`;
  });

  select.innerHTML = html;
}

function handleFilesRunSelect(e) {
  const runId = e.target.value;
  if (runId) {
    inspectFilesForRun(runId);
  } else {
    currentFilesRunId = null;
    document.getElementById('files-table-body').innerHTML = `<tr><td colspan="4" class="px-6 py-8 text-center text-slate-500">Select a notebook run to view generated output artifacts and files.</td></tr>`;
  }
}

async function inspectFilesForRun(runId) {
  currentFilesRunId = runId;
  AppState.selectedFilesRunId = runId;
  updateFilesRunDropdown();

  const tbody = document.getElementById('files-table-body');
  tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-8 text-center text-slate-400"><i data-lucide="loader-2" class="w-5 h-5 animate-spin mx-auto mb-2 text-cyan-400"></i> Querying Kaggle for output files...</td></tr>`;
  refreshIcons();

  try {
    const res = await fetch(`/api/runs/${runId}/files`);
    const data = await res.json();

    if (data.success) {
      renderFilesTable(data);
    } else {
      tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-8 text-center text-rose-400">Failed to load files: ${data.detail || 'Unknown error'}</td></tr>`;
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-8 text-center text-rose-400">Error: ${err.message}</td></tr>`;
  }
}

function renderFilesTable(data) {
  const tbody = document.getElementById('files-table-body');
  const remoteFiles = data.remote_files || [];
  const localFiles = data.local_files || [];

  const combined = [];
  const seen = new Set();

  remoteFiles.forEach(rf => {
    seen.add(rf.name || rf.fileName);
    combined.push({
      name: rf.name || rf.fileName,
      size: rf.size || 'N/A',
      isLocal: false
    });
  });

  localFiles.forEach(lf => {
    if (!seen.has(lf.name)) {
      combined.push({
        name: lf.name,
        size: formatBytes(lf.size),
        isLocal: true
      });
    }
  });

  if (combined.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="4" class="px-6 py-8 text-center text-slate-500">
          No output files detected for this run yet.<br>
          <span class="text-xs text-slate-600">If the notebook just finished, click "Pull Output Files from Kaggle" above.</span>
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = combined.map(f => {
    const ext = f.name.split('.').pop().toUpperCase();
    return `
      <tr class="hover:bg-slate-800/30 transition">
        <td class="px-6 py-3.5 font-mono text-xs font-semibold text-white flex items-center space-x-2">
          <i data-lucide="file" class="w-4 h-4 text-amber-400"></i>
          <span>${esc(f.name)}</span>
        </td>
        <td class="px-6 py-3.5 text-xs text-slate-400 font-mono">${f.size}</td>
        <td class="px-6 py-3.5">
          <span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700">${ext}</span>
        </td>
        <td class="px-6 py-3.5 text-right">
          <a href="/api/runs/${currentFilesRunId}/files/download/${encodeURIComponent(f.name)}" download class="inline-flex items-center space-x-1 px-2.5 py-1 rounded text-xs font-semibold bg-cyan-600/20 text-cyan-400 hover:bg-cyan-600/30 transition">
            <i data-lucide="download" class="w-3.5 h-3.5"></i>
            <span>Download</span>
          </a>
        </td>
      </tr>
    `;
  }).join('');
  refreshIcons();
}

async function pullRemoteFiles() {
  if (!currentFilesRunId) {
    showToast('Select a run first to pull output files', 'warning');
    return;
  }

  showToast('Downloading all output artifacts from Kaggle CLI...', 'info');
  const btn = document.getElementById('btn-pull-files');
  btn.disabled = true;

  try {
    const res = await fetch(`/api/runs/${currentFilesRunId}/files/pull`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      inspectFilesForRun(currentFilesRunId);
    } else {
      showToast(data.error || 'Failed to pull outputs', 'error');
    }
  } catch (err) {
    showToast('Error pulling files: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function downloadAllFilesZip() {
  if (!currentFilesRunId) {
    showToast('Select a run first to download ZIP', 'warning');
    return;
  }
  showToast('Preparing ZIP package...', 'info');
  window.open(`/api/runs/${currentFilesRunId}/files/download-zip`, '_blank');
}

function formatBytes(bytes, decimals = 2) {
  if (!+bytes) return '0 Bytes';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}
