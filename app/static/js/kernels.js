// Account Kernels Explorer - browses any notebook per account (even not from dashboard)

let kernelsState = {
  account: null,
  page: 1,
  pageSize: 20,
  search: "",
  kernels: [],
  selected: null, // {account, ref, title}
};

function populateKernelsAccountSelect() {
  try {
    const sel = document.getElementById('kernels-account-select');
    if (!sel) return;
    const prev = sel.value;
    const accounts = (typeof AppState !== 'undefined' && AppState.accounts) ? AppState.accounts : [];
    let html = '<option value="">-- Select Account --</option>';
    if (!accounts.length) {
      html += '<option value="" disabled>Loading accounts... (if stuck, click ↻)</option>';
    } else {
      html += accounts.map(acc => {
        const selAttr = acc.username === prev ? 'selected' : '';
        return `<option value="${esc(acc.username)}" ${selAttr}>@${esc(acc.username)}</option>`;
      }).join('');
    }
    sel.innerHTML = html;
  } catch (e) {
    console.error('populateKernelsAccountSelect failed', e);
  }
}

async function fetchAndPopulateKernelsAccounts() {
  try {
    const res = await fetch('/api/accounts', { credentials: 'same-origin' });
    if (res.status === 401) return false;
    const data = await res.json();
    if (data.success && Array.isArray(data.accounts) && data.accounts.length) {
      if (typeof AppState !== 'undefined') AppState.accounts = data.accounts;
      populateKernelsAccountSelect();
      return true;
    }
  } catch (e) {
    console.error('fetchAndPopulateKernelsAccounts failed', e);
  }
  return false;
}

function initKernelsTab() {
  populateKernelsAccountSelect();
  // Always ensure accounts are loaded - direct fetch is more reliable than relying on AppState timing
  fetchAndPopulateKernelsAccounts();
  // Retry once after 1.5s in case first fetch raced with AppState refresh
  setTimeout(() => {
    const sel = document.getElementById('kernels-account-select');
    if (sel && sel.options.length <= 2) {
      fetchAndPopulateKernelsAccounts();
    }
  }, 1500);
  updateKernelsPaginationUI();
  try { refreshIcons(); } catch (_) {}
}

function updateKernelsPaginationUI() {
  const info = document.getElementById('kernels-page-info');
  const prevBtn = document.getElementById('btn-kernels-prev');
  const nextBtn = document.getElementById('btn-kernels-next');
  if (info) info.textContent = `Page ${kernelsState.page}`;
  if (prevBtn) prevBtn.disabled = kernelsState.page <= 1;
  // next disabled until we know there is no next page - keep enabled, will disable after fetch if no token
  if (nextBtn && !nextBtn.dataset.hasNext) nextBtn.disabled = true;
}

function loadKernelsPage(delta) {
  kernelsState.page = Math.max(1, kernelsState.page + delta);
  loadKernelsForAccount();
}

function setBtnLoading(btn, loading, loadingText) {
  if (!btn) return;
  if (loading) {
    btn.dataset.origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>${esc(loadingText || 'Loading...')}</span>`;
    try { refreshIcons(); } catch (_) {}
  } else {
    btn.disabled = false;
    if (btn.dataset.origHtml) btn.innerHTML = btn.dataset.origHtml;
    try { refreshIcons(); } catch (_) {}
  }
}

async function loadKernelsForAccount() {
  const sel = document.getElementById('kernels-account-select');
  const searchEl = document.getElementById('kernels-search');
  const tbody = document.getElementById('kernels-table-body');
  const countEl = document.getElementById('kernels-count');
  const loadBtn = document.getElementById('btn-load-kernels');
  if (!sel || !tbody) return;

  const account = sel.value.trim();
  if (!account) {
    showToast('Select an account first', 'warning');
    return;
  }
  const newSearch = (searchEl ? searchEl.value.trim() : "");
  const accountChanged = kernelsState.account !== account;
  const searchChanged = kernelsState.search !== newSearch;
  if (accountChanged || searchChanged) {
    kernelsState.page = 1;
  }
  kernelsState.account = account;
  kernelsState.search = newSearch;

  tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-slate-400"><i data-lucide="loader-2" class="w-5 h-5 animate-spin mx-auto mb-2 text-indigo-400"></i> Listing notebooks for @${esc(account)}...</td></tr>`;
  refreshIcons();
  if (countEl) countEl.textContent = '';
  const nextBtn = document.getElementById('btn-kernels-next');
  if (nextBtn) nextBtn.disabled = true;
  setBtnLoading(loadBtn, true, 'Loading...');

  try {
    const params = new URLSearchParams({ account, search: kernelsState.search, page: String(kernelsState.page), pageSize: String(kernelsState.pageSize) });
    const res = await fetch(`/api/kernels/list?${params.toString()}`);
    if (res.status === 401) { window.location.href = '/login'; return; }
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'Failed to list kernels');
    // Ensure reverse chronological (newest lastRunTime first) even if backend didn't sort
    let kernels = data.kernels || [];
    try {
      kernels.sort((a, b) => {
        const ta = a.lastRunTime || a.creationTime || '';
        const tb = b.lastRunTime || b.creationTime || '';
        return new Date(tb) - new Date(ta);
      });
    } catch (_) {}
    kernelsState.kernels = kernels;
    const hasNext = !!(data.nextPageToken);
    if (nextBtn) {
      nextBtn.disabled = !hasNext;
      nextBtn.dataset.hasNext = hasNext ? '1' : '';
    }
    updateKernelsPaginationUI();
    if (countEl) countEl.textContent = `${kernelsState.kernels.length} notebook(s)`;
    renderKernelsTable();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-rose-400">Error: ${esc(err.message)}</td></tr>`;
  } finally {
    setBtnLoading(loadBtn, false);
  }
}

function isTerminalStatus(st) {
  st = (st || '').toLowerCase();
  return st === 'complete' || st === 'error' || st === 'stopped' || st === 'failed' || st === 'cancelled' || st === 'cancelacknowledged' || st === 'cancel_acknowledged';
}
function updateStopButtonState(account, ref, status) {
  const terminal = isTerminalStatus(status);
  // Per-row stop buttons (filter by data-ref to avoid CSS.escape issues with slash)
  document.querySelectorAll('button[data-ref]').forEach(btn => {
    if (btn.dataset.ref === ref && btn.getAttribute('onclick') && btn.getAttribute('onclick').includes('kernelsStopRow')) {
      btn.disabled = terminal;
      btn.classList.toggle('opacity-40', terminal);
      btn.classList.toggle('cursor-not-allowed', terminal);
      btn.title = terminal ? `Already ${status} — stop not needed` : 'Stop this kernel if running';
      if (terminal) btn.classList.add('opacity-60'); else btn.classList.remove('opacity-60');
    }
  });
  // Detail panel stop button if selected matches
  if (kernelsState.selected && kernelsState.selected.ref === ref) {
    const detailBtn = document.getElementById('btn-kernels-stop');
    if (detailBtn) {
      detailBtn.disabled = terminal;
      detailBtn.classList.toggle('opacity-40', terminal);
      detailBtn.classList.toggle('cursor-not-allowed', terminal);
      detailBtn.title = terminal ? `Already ${status}` : 'Stop this kernel';
    }
  }
}
function renderKernelsTable() {
  const tbody = document.getElementById('kernels-table-body');
  if (!tbody) return;
  if (!kernelsState.kernels.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-slate-500">No notebooks found for this account (or search returned nothing).</td></tr>`;
    return;
  }
  tbody.innerHTML = kernelsState.kernels.map((k, idx) => {
    const ref = k.ref || '';
    const title = k.title || k.slug || ref;
    const lastRun = k.lastRunTime || k.creationTime || '';
    const lastRunDisplay = lastRun ? formatKernelsTime(lastRun) : '<span class="text-slate-500">—</span>';
    const version = k.currentVersionNumber != null ? `v${k.currentVersionNumber}` : '—';
    const statusCellId = `kstatus-${idx}`;
    return `
      <tr class="hover:bg-slate-800/30 transition">
        <td class="px-4 py-3">
          <div class="font-bold text-white text-xs truncate max-w-[260px]">${esc(title)}</div>
          <a href="https://www.kaggle.com/code/${encodeURIComponent(ref)}" target="_blank" class="text-[11px] font-mono text-indigo-400 hover:underline">${esc(ref)}</a>
        </td>
        <td class="px-4 py-3" id="${statusCellId}">
          <button onclick="fetchKernelStatus('${esc(kernelsState.account)}','${esc(ref)}','${statusCellId}')" class="px-2 py-1 rounded text-[10px] font-bold bg-slate-800 text-slate-300 hover:bg-slate-700 border border-slate-700">Check</button>
        </td>
        <td class="px-4 py-3 text-xs font-mono text-slate-300 whitespace-nowrap" title="${esc(lastRun)}">${lastRunDisplay}</td>
        <td class="px-4 py-3 text-xs font-mono text-slate-400">${esc(version)}</td>
        <td class="px-4 py-3 text-right">
          <div class="flex items-center justify-end gap-2 flex-wrap">
            <button data-account="${esc(kernelsState.account)}" data-ref="${esc(ref)}" data-title="${esc(title)}" onclick="openKernelsDetail(this.dataset.account, this.dataset.ref, this.dataset.title)" class="px-2.5 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600/20 text-indigo-300 hover:bg-indigo-600/30 border border-indigo-500/30 transition">Open</button>
            <button id="kstop-${idx}" data-account="${esc(kernelsState.account)}" data-ref="${esc(ref)}" data-title="${esc(title)}" onclick="kernelsStopRow(this.dataset.account, this.dataset.ref, this.dataset.title, this)" title="Stop this kernel if running" class="px-2 py-1 rounded text-xs bg-rose-600/20 text-rose-300 hover:bg-rose-600/30 border border-rose-500/30 disabled:opacity-40 disabled:cursor-not-allowed opacity-60" disabled>Stop</button>
            <a href="https://www.kaggle.com/code/${encodeURIComponent(ref)}" target="_blank" class="px-2 py-1 rounded text-xs bg-slate-800 text-slate-400 hover:text-white border border-slate-700"><i data-lucide="external-link" class="w-3 h-3 inline"></i></a>
          </div>
        </td>
      </tr>
    `;
  }).join('');
  refreshIcons();
  // Auto-fetch status for first 6 rows with staggered calls to avoid burst (backend throttles to 3 anyway)
  kernelsState.kernels.slice(0, 6).forEach((k, idx) => {
    setTimeout(() => fetchKernelStatus(kernelsState.account, k.ref, `kstatus-${idx}`), idx * 400);
  });
}
async function kernelsStopRow(account, ref, title, btn) {
  if (!confirm(`Stop kernel ${ref} on @${account}?`)) return;
  setBtnLoading(btn, true, 'Stopping...');
  try {
    const res = await fetch('/api/kernels/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_username: account, kernel_ref: ref, title })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Stop signal sent', 'success');
      // Find and refresh status cell for this ref
      const idx = kernelsState.kernels.findIndex(k => k.ref === ref);
      if (idx >= 0) fetchKernelStatus(account, ref, `kstatus-${idx}`);
    } else {
      throw new Error(data.detail || data.error || 'Stop failed');
    }
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    setBtnLoading(btn, false);
  }
}

function formatKernelsTime(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);
    let ago = '';
    if (diffMin < 60) ago = `${diffMin}m ago`;
    else if (diffMin < 1440) ago = `${Math.floor(diffMin / 60)}h ago`;
    else ago = `${Math.floor(diffMin / 1440)}d ago`;
    return `${esc(d.toLocaleString())} <span class="text-slate-500">(${ago})</span>`;
  } catch (_) { return esc(iso); }
}

async function fetchKernelStatus(account, kernelRef, cellId) {
  const el = document.getElementById(cellId);
  if (!el) return;
  el.innerHTML = `<span class="text-[10px] text-slate-500"><i data-lucide="loader-2" class="w-3 h-3 animate-spin inline"></i> ...</span>`;
  refreshIcons();
  try {
    const params = new URLSearchParams({ account, kernel_ref: kernelRef });
    const res = await fetch(`/api/kernels/status?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'status failed');
    const st = (data.status || 'unknown').toLowerCase();
    let cls = 'bg-slate-800 text-slate-300 border-slate-700';
    if (st === 'running' || st === 'queued') cls = 'bg-amber-950 text-amber-300 border-amber-800';
    else if (st === 'complete') cls = 'bg-emerald-950 text-emerald-300 border-emerald-800';
    else if (st === 'error') cls = 'bg-rose-950 text-rose-300 border-rose-800';
    else if (st === 'stopped') cls = 'bg-slate-800 text-slate-400 border-slate-700';
    el.innerHTML = `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold border ${cls}">${esc(st.toUpperCase())}</span>`;
    // Grey out stop if already terminal
    updateStopButtonState(account, kernelRef, st);
  } catch (err) {
    el.innerHTML = `<span class="text-[10px] text-rose-400">err</span>`;
  }
}

function openKernelsDetail(account, ref, title) {
  kernelsState.selected = { account, ref, title };
  const panel = document.getElementById('kernels-detail-panel');
  const titleEl = document.getElementById('kernels-detail-title');
  const refEl = document.getElementById('kernels-detail-ref');
  if (titleEl) titleEl.textContent = title || ref;
  if (refEl) refEl.textContent = ref;
  if (panel) panel.classList.remove('hidden');
  // Stop button initially disabled until we know status is running
  const stopBtn = document.getElementById('btn-kernels-stop');
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.classList.add('opacity-40', 'cursor-not-allowed');
    stopBtn.title = 'Checking status...';
  }
  // Auto-refresh files and logs preview
  kernelsListFiles();
  document.getElementById('kernels-logs-pre').textContent = 'Fetching logs...';
  kernelsFetchLogs();
  // Also fetch status for this kernel to correctly enable/disable stop
  fetch(`/api/kernels/status?account=${encodeURIComponent(account)}&kernel_ref=${encodeURIComponent(ref)}`)
    .then(r => r.json()).then(d => {
      if (d.success) updateStopButtonState(account, ref, d.status);
    }).catch(()=>{});
  refreshIcons();
  // Scroll to detail
  if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeKernelsDetail() {
  const panel = document.getElementById('kernels-detail-panel');
  if (panel) panel.classList.add('hidden');
  kernelsState.selected = null;
}

async function kernelsListFiles() {
  if (!kernelsState.selected) { showToast('Open a kernel first','warning'); return; }
  const { account, ref } = kernelsState.selected;
  const tbody = document.getElementById('kernels-files-body');
  const btn = document.getElementById('btn-kernels-files');
  if (tbody) tbody.innerHTML = `<tr><td colspan="3" class="px-3 py-6 text-center text-slate-400"><i data-lucide="loader-2" class="w-4 h-4 animate-spin mx-auto mb-1"></i> Listing files...</td></tr>`;
  refreshIcons();
  setBtnLoading(btn, true, 'Listing...');
  try {
    const params = new URLSearchParams({ account, kernel_ref: ref });
    const res = await fetch(`/api/kernels/files?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'failed');
    renderKernelsFiles(data);
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="3" class="px-3 py-6 text-center text-rose-400">${esc(err.message)}</td></tr>`;
  } finally {
    setBtnLoading(btn, false);
  }
}

function renderKernelsFiles(data) {
  const tbody = document.getElementById('kernels-files-body');
  if (!tbody) return;
  const local = data.local_files || [];
  const remote = data.remote_files || [];
  // Prefer local if exists, else show remote as hint
  const rows = local.length ? local : remote.map(r => ({ name: r.name || r.fileName, size: r.size || '—', rel_path: r.name || r.fileName, isRemote: true }));
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="px-3 py-6 text-center text-slate-500">No output files yet. If it just finished, click Pull.</td></tr>`;
    return;
  }
  const fmt = (typeof formatBytes === 'function') ? formatBytes : (b => String(b));
  tbody.innerHTML = rows.map(f => {
    const name = f.name || f.fileName || f.rel_path;
    const size = f.size != null ? (typeof f.size === 'number' ? fmt(f.size) : esc(String(f.size))) : '—';
    const isRemote = !!f.isRemote;
    const dlUrl = `/api/kernels/files/download/${encodeURIComponent(name)}?account=${encodeURIComponent(kernelsState.selected.account)}&kernel_ref=${encodeURIComponent(kernelsState.selected.ref)}`;
    return `
      <tr class="hover:bg-slate-800/30">
        <td class="px-3 py-2 font-mono text-xs text-white truncate max-w-[200px]">${esc(name)} ${isRemote ? '<span class="text-[9px] px-1 py-0.5 rounded bg-sky-900/50 text-sky-300 border border-sky-800">REMOTE</span>' : '<span class="text-[9px] px-1 py-0.5 rounded bg-emerald-900/50 text-emerald-300 border border-emerald-800">LOCAL</span>'}</td>
        <td class="px-3 py-2 text-xs font-mono text-slate-400">${size}</td>
        <td class="px-3 py-2 text-right">
          ${isRemote ? '<span class="text-[10px] text-slate-500">pull to download</span>' : `<a href="${dlUrl}" download class="inline-flex items-center px-2 py-1 rounded text-xs bg-cyan-600/20 text-cyan-300 hover:bg-cyan-600/30 border border-cyan-700/50">Download</a>`}
        </td>
      </tr>
    `;
  }).join('');
  refreshIcons();
}

async function kernelsPullOutput(version) {
  if (!kernelsState.selected) return;
  const { account, ref } = kernelsState.selected;
  const isVersion = version != null;
  const btn = isVersion ? document.getElementById(`btn-pull-v${version}`) : document.getElementById('btn-kernels-pull');
  setBtnLoading(btn, true, isVersion ? `Pulling v${version}...` : 'Pulling...');
  showToast(`Pulling ${isVersion ? 'v'+version+' of ' : ''}${ref}...`, 'info');
  try {
    const payload = { account_username: account, kernel_ref: ref };
    if (isVersion) payload.version = parseInt(version, 10);
    const res = await fetch('/api/kernels/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || data.error || 'pull failed');
    showToast(data.message || 'Pulled', 'success');
    kernelsListFiles();
    if (isVersion) loadKernelVersions();
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    setBtnLoading(btn, false);
  }
}
function kernelsPullVersion(v) { return kernelsPullOutput(v); }

async function kernelsFetchLogs() {
  if (!kernelsState.selected) return;
  const { account, ref } = kernelsState.selected;
  const pre = document.getElementById('kernels-logs-pre');
  const btn = document.getElementById('btn-kernels-logs');
  if (pre) pre.textContent = 'Fetching logs from Kaggle...';
  setBtnLoading(btn, true, 'Fetching...');
  try {
    const params = new URLSearchParams({ account, kernel_ref: ref });
    const res = await fetch(`/api/kernels/logs?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'failed');
    const logs = data.logs || '';
    if (pre) pre.textContent = logs ? logs : '(no logs returned)';
  } catch (err) {
    if (pre) pre.textContent = 'Error: ' + err.message;
  } finally {
    setBtnLoading(btn, false);
  }
}

function kernelsDownloadZip() {
  if (!kernelsState.selected) { showToast('Open a kernel first','warning'); return; }
  const { account, ref } = kernelsState.selected;
  const params = new URLSearchParams({ account, kernel_ref: ref });
  window.open(`/api/kernels/files/download-zip?${params.toString()}`, '_blank');
}

async function loadKernelVersions() {
  if (!kernelsState.selected) { showToast('Open a kernel first','warning'); return; }
  const { account, ref } = kernelsState.selected;
  const tbody = document.getElementById('kernels-versions-body');
  const btn = document.getElementById('btn-load-versions');
  if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="px-3 py-6 text-center text-slate-400"><i data-lucide="loader-2" class="w-4 h-4 animate-spin mx-auto mb-1"></i> Loading version history...</td></tr>`;
  setBtnLoading(btn, true, 'Loading...');
  try { refreshIcons(); } catch (_) {}
  try {
    const params = new URLSearchParams({ account, kernel_ref: ref, max_versions: '20' });
    const res = await fetch(`/api/kernels/versions?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'failed');
    renderKernelVersions(data.versions || []);
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="px-3 py-6 text-center text-rose-400">Error: ${esc(err.message)}</td></tr>`;
  } finally {
    setBtnLoading(btn, false);
  }
}

function renderKernelVersions(versions) {
  const tbody = document.getElementById('kernels-versions-body');
  if (!tbody) return;
  if (!versions.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="px-3 py-6 text-center text-slate-500">No version history found (kernel may be new or API didn't return versions). Try pulling latest output instead.</td></tr>`;
    return;
  }
  tbody.innerHTML = versions.map(v => {
    const timeDisplay = v.creationTime ? formatKernelsTime(v.creationTime) : '<span class="text-slate-500">—</span>';
    const status = (v.status || 'unknown').toLowerCase();
    let statusCls = 'bg-slate-800 text-slate-300 border-slate-700';
    if (status === 'complete') statusCls = 'bg-emerald-950 text-emerald-300 border-emerald-800';
    else if (status === 'running' || status === 'queued') statusCls = 'bg-amber-950 text-amber-300 border-amber-800';
    else if (status === 'error') statusCls = 'bg-rose-950 text-rose-300 border-rose-800';
    else if (status === 'stopped') statusCls = 'bg-slate-800 text-slate-400 border-slate-700';
    const hasOutput = v.hasOutput ? `<span class="text-emerald-400">${v.fileCount} files</span>` : `<span class="text-slate-500">log only</span>`;
    return `
      <tr class="hover:bg-slate-800/30">
        <td class="px-3 py-2 font-mono text-xs font-bold text-white">v${v.version}</td>
        <td class="px-3 py-2 text-xs font-mono text-slate-300 whitespace-nowrap" title="${esc(v.creationTime || '')}">${timeDisplay}</td>
        <td class="px-3 py-2"><span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold border ${statusCls}">${esc(status.toUpperCase())}</span></td>
        <td class="px-3 py-2 text-xs text-slate-400">${hasOutput}</td>
        <td class="px-3 py-2 text-right">
          <div class="flex items-center justify-end gap-1">
            <button id="btn-pull-v${v.version}" onclick="kernelsPullVersion(${v.version})" class="px-2 py-1 rounded text-xs font-bold bg-indigo-600/20 text-indigo-300 hover:bg-indigo-600/30 border border-indigo-500/30">Pull</button>
            <button id="btn-log-v${v.version}" onclick="kernelsFetchVersionLog(${v.version})" class="px-2 py-1 rounded text-xs bg-slate-800 text-slate-400 hover:text-white border border-slate-700" title="Fetch log for this version">Logs</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
  try { refreshIcons(); } catch (_) {}
}
async function kernelsFetchVersionLog(version) {
  if (!kernelsState.selected) return;
  const { account, ref } = kernelsState.selected;
  const pre = document.getElementById('kernels-logs-pre');
  const btn = document.getElementById(`btn-log-v${version}`);
  if (pre) pre.textContent = `Fetching log for v${version}...`;
  setBtnLoading(btn, true, `v${version}...`);
  try {
    const params = new URLSearchParams({ account, kernel_ref: ref, version: String(version) });
    const res = await fetch(`/api/kernels/logs?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'failed');
    if (pre) pre.textContent = data.logs ? `--- Version ${version} log ---\n` + data.logs : `(no log for v${version})`;
    pre.scrollTop = 0;
  } catch (err) {
    if (pre) pre.textContent = 'Error: ' + err.message;
  } finally {
    setBtnLoading(btn, false);
  }
}

async function kernelsStop() {
  if (!kernelsState.selected) { showToast('Open a kernel first','warning'); return; }
  const { account, ref, title } = kernelsState.selected;
  if (!confirm(`Stop kernel ${ref} on @${account}?\nThis pushes a 1-sec cancel stub.`)) return;
  const btn = document.getElementById('btn-kernels-stop');
  setBtnLoading(btn, true, 'Stopping...');
  showToast(`Sending stop signal to ${ref}...`, 'warning');
  try {
    const res = await fetch('/api/kernels/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_username: account, kernel_ref: ref, title })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Stop signal sent', 'success');
      // Refresh status for this kernel
      const idx = kernelsState.kernels.findIndex(k => k.ref === ref);
      if (idx >= 0) fetchKernelStatus(account, ref, `kstatus-${idx}`);
    } else {
      throw new Error(data.detail || data.error || 'Stop failed');
    }
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    setBtnLoading(btn, false);
  }
}
