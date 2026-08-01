// Output Files Explorer Module (+ Merge Cart & local file deletion)

let currentFilesRunId = null;
let currentFileRows = []; // last rendered rows [{name, size, sizeBytes, isLocal}]

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
    currentFileRows = [];
    document.getElementById('files-table-body').innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-slate-500">Select a notebook run to view generated output artifacts and files.</td></tr>`;
  }
}

async function inspectFilesForRun(runId) {
  currentFilesRunId = runId;
  AppState.selectedFilesRunId = runId;
  updateFilesRunDropdown();

  const tbody = document.getElementById('files-table-body');
  tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-slate-400"><i data-lucide="loader-2" class="w-5 h-5 animate-spin mx-auto mb-2 text-cyan-400"></i> Querying Kaggle for output files...</td></tr>`;
  refreshIcons();

  try {
    const res = await fetch(`/api/runs/${runId}/files`);
    if (res.status === 401) { window.location.href = '/login'; return; }
    const data = await res.json();

    if (data.success) {
      renderFilesTable(data);
    } else {
      tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-rose-400">Failed to load files: ${data.detail || 'Unknown error'}</td></tr>`;
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-rose-400">Error: ${err.message}</td></tr>`;
  }
}

function isInCart(runId, filename) {
  return (AppState.cartFiles || []).some(i => i.run_id === runId && i.filename === filename);
}

function renderFilesTable(data) {
  const tbody = document.getElementById('files-table-body');
  const remoteFiles = data.remote_files || [];
  const localFiles = data.local_files || [];

  const combined = [];
  const seen = new Set();

  remoteFiles.forEach(rf => {
    const name = rf.name || rf.fileName;
    seen.add(name);
    combined.push({
      name,
      size: rf.size || 'N/A',
      sizeBytes: Number(rf.size) || 0,
      isLocal: false
    });
  });

  localFiles.forEach(lf => {
    if (!seen.has(lf.name)) {
      combined.push({
        name: lf.name,
        relPath: lf.rel_path,
        size: formatBytes(lf.size),
        sizeBytes: lf.size,
        isLocal: true
      });
    }
  });
  currentFileRows = combined;

  if (combined.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" class="px-6 py-8 text-center text-slate-500">
          No output files detected for this run yet.<br>
          <span class="text-xs text-slate-600">If the notebook just finished, click "Pull Output Files from Kaggle" above.<br>
          Notebooks that were manually stopped keep whatever was snapshotted at stop time.</span>
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = combined.map(f => {
    const ext = f.name.split('.').pop().toUpperCase();
    const checked = isInCart(currentFilesRunId, f.name);
    return `
      <tr class="hover:bg-slate-800/30 transition">
        <td class="px-4 py-3.5 align-middle">
          <input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleCartFile('${esc(currentFilesRunId)}', '${esc(f.name)}', this.checked)"
                 class="w-4 h-4 text-emerald-500 rounded bg-slate-900 border-slate-700 align-middle cursor-pointer"
                 title="Add to Merge Cart">
        </td>
        <td class="px-6 py-3.5 font-mono text-xs font-semibold text-white">
          <div class="flex items-center space-x-2 min-w-0">
            <i data-lucide="file" class="w-4 h-4 text-amber-400 flex-shrink-0"></i>
            <span class="truncate">${esc(f.name)}</span>
            ${!f.isLocal
              ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-sky-900/60 text-sky-300 border border-sky-800 flex-shrink-0" title="Listed on Kaggle - not downloaded yet">REMOTE</span>'
              : '<span class="text-[9px] px-1.5 py-0.5 rounded bg-emerald-900/60 text-emerald-300 border border-emerald-800 flex-shrink-0" title="Stored on this server (auto-synced at completion or snapshotted at stop)">LOCAL</span>'}
          </div>
        </td>
        <td class="px-6 py-3.5 text-xs text-slate-400 font-mono whitespace-nowrap">${f.size}</td>
        <td class="px-6 py-3.5">
          <span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700">${ext}</span>
        </td>
        <td class="px-6 py-3.5">
          <div class="flex items-center justify-end gap-2 flex-wrap">
            <a href="/api/runs/${currentFilesRunId}/files/download/${encodeURIComponent(f.name)}" download class="inline-flex items-center space-x-1 px-2.5 py-1 rounded text-xs font-semibold bg-cyan-600/20 text-cyan-400 hover:bg-cyan-600/30 transition">
              <i data-lucide="download" class="w-3.5 h-3.5"></i>
              <span>Download</span>
            </a>
            ${f.isLocal ? `
            <button onclick="deleteSingleOutputFile('${esc(f.name)}')" title="Delete from server storage"
                    class="inline-flex items-center space-x-1 px-2.5 py-1 rounded text-xs font-semibold bg-rose-600/20 text-rose-400 hover:bg-rose-600/40 transition">
              <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
              <span>Delete</span>
            </button>` : ''}
          </div>
        </td>
      </tr>
    `;
  }).join('');
  refreshIcons();
}

function setFilesBtnLoading(btn, loading, text) {
  if (!btn) return;
  if (loading) {
    btn.dataset.origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>${text || 'Loading...'}</span>`;
    try { refreshIcons(); } catch (_) {}
  } else {
    btn.disabled = false;
    if (btn.dataset.origHtml) btn.innerHTML = btn.dataset.origHtml;
    try { refreshIcons(); } catch (_) {}
  }
}
async function pullRemoteFiles() {
  if (!currentFilesRunId) {
    showToast('Select a run first to pull output files', 'warning');
    return;
  }
  showToast('Downloading all output artifacts from Kaggle CLI...', 'info');
  const btn = document.getElementById('btn-pull-files');
  setFilesBtnLoading(btn, true, 'Pulling...');
  try {
    const res = await fetch(`/api/runs/${currentFilesRunId}/files/pull`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      inspectFilesForRun(currentFilesRunId);
    } else {
      showToast(data.error || data.detail || 'Failed to pull outputs', 'error');
    }
  } catch (err) {
    showToast('Error pulling files: ' + err.message, 'error');
  } finally {
    setFilesBtnLoading(btn, false);
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

async function deleteSingleOutputFile(filename) {
  if (!currentFilesRunId) return;
  if (!confirm(`Delete "${filename}" from the server's local outputs?\n(The copy on Kaggle is not touched.)`)) return;
  try {
    const res = await fetch(`/api/runs/${currentFilesRunId}/files/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      removeFromCart(currentFilesRunId, filename, false);
      showToast(data.message, 'success');
      inspectFilesForRun(currentFilesRunId);
    } else {
      showToast(data.detail || 'Failed to delete file', 'error');
    }
  } catch (err) {
    showToast('Error deleting file: ' + err.message, 'error');
  }
}

async function deleteAllLocalFiles() {
  if (!currentFilesRunId) {
    showToast('Select a run first', 'warning');
    return;
  }
  if (!confirm('Delete ALL locally downloaded output files for this run?\n(Files stay safe on Kaggle - you can re-pull them anytime.)')) return;
  try {
    const res = await fetch(`/api/runs/${currentFilesRunId}/files`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      AppState.cartFiles = (AppState.cartFiles || []).filter(i => i.run_id !== currentFilesRunId);
      renderMergeCart();
      showToast(data.message, 'success');
      inspectFilesForRun(currentFilesRunId);
    } else {
      showToast(data.detail || 'Failed to delete output files', 'error');
    }
  } catch (err) {
    showToast('Error deleting output files: ' + err.message, 'error');
  }
}

// ------------------------------------------------------------------
// Merge Cart - collect individual output files across finished runs
// ------------------------------------------------------------------
function toggleCartFile(runId, filename, checked) {
  AppState.cartFiles = AppState.cartFiles || [];
  const exists = AppState.cartFiles.some(i => i.run_id === runId && i.filename === filename);
  if (checked && !exists) {
    const run = (AppState.allRuns || []).find(r => r.id === runId);
    AppState.cartFiles.push({
      run_id: runId,
      filename,
      label: run ? `${run.title} [@${run.account_username}]` : runId
    });
    showToast(`Added to cart (${AppState.cartFiles.length})`, 'info');
  } else if (!checked && exists) {
    AppState.cartFiles = AppState.cartFiles.filter(i => !(i.run_id === runId && i.filename === filename));
  }
  renderMergeCart();
}

function removeFromCart(runId, filename, rerenderRows = true) {
  AppState.cartFiles = (AppState.cartFiles || []).filter(i => !(i.run_id === runId && i.filename === filename));
  renderMergeCart();
  if (rerenderRows && currentFilesRunId === runId) inspectFilesForRun(runId);
}

function toggleSelectAllFiles(checked) {
  currentFileRows.forEach(f => {
    if (currentFilesRunId) toggleCartFile(currentFilesRunId, f.name, checked);
  });
}

function clearMergeCart() {
  if (!(AppState.cartFiles || []).length) return;
  AppState.cartFiles = [];
  renderMergeCart();
  if (currentFilesRunId) inspectFilesForRun(currentFilesRunId);
  showToast('Cart cleared', 'info');
}

function renderMergeCart() {
  const badge = document.getElementById('cart-count-badge');
  const list = document.getElementById('cart-items-list');
  const dlBtn = document.getElementById('btn-download-merged');
  if (!badge || !list) return;

  const cart = AppState.cartFiles || [];
  badge.textContent = `${cart.length} file${cart.length !== 1 ? 's' : ''}`;
  dlBtn.disabled = cart.length === 0;

  if (cart.length === 0) {
    list.innerHTML = `<p class="text-xs text-slate-500 text-center py-4">Cart is empty. Select files from the table above to build your merged download.</p>`;
    return;
  }

  list.innerHTML = cart.map((item, idx) => `
    <div class="flex items-center justify-between gap-3 p-2.5 rounded-lg bg-[#080b12] border border-[#1e293b]">
      <div class="min-w-0">
        <p class="text-xs font-mono font-semibold text-white truncate">${esc(item.filename)}</p>
        <p class="text-[10px] text-slate-400 truncate">${esc(item.label)}</p>
      </div>
      <button onclick="removeFromCart('${esc(item.run_id)}', '${esc(item.filename)}')" class="text-slate-500 hover:text-rose-400 transition flex-shrink-0 p-1">
        <i data-lucide="x" class="w-3.5 h-3.5"></i>
      </button>
    </div>
  `).join('');
  refreshIcons();
}

async function downloadMergedCart() {
  const cart = AppState.cartFiles || [];
  if (cart.length === 0) {
    showToast('Cart is empty', 'warning');
    return;
  }

  const btn = document.getElementById('btn-download-merged');
  btn.disabled = true;
  showToast(`Concatenating ${cart.length} file(s)...`, 'info');

  try {
    const res = await fetch('/api/runs/files/merge-download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: cart.map(i => ({ run_id: i.run_id, filename: i.filename })) })
    });

    if (!res.ok) {
      let detail = 'Merge failed';
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      showToast(detail, 'error');
      return;
    }

    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/);
    const filename = match ? match[1] : 'merged.bin';

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    showToast(`Merged file downloaded (${filename})`, 'success');
  } catch (err) {
    showToast('Error merging files: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function formatBytes(bytes, decimals = 2) {
  if (!+bytes) return '0 Bytes';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}
