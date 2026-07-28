// Account Kernels Explorer - browses any notebook per account (even not from dashboard)

let kernelsState = {
  account: null,
  page: 1,
  pageSize: 20,
  search: "",
  kernels: [],
  selected: null, // {account, ref, title}
};

function initKernelsTab() {
  // Populate account dropdown from global state
  const sel = document.getElementById('kernels-account-select');
  if (!sel) return;
  const prev = sel.value;
  let html = '<option value="">-- Select Account --</option>';
  (AppState.accounts || []).forEach(acc => {
    const selAttr = acc.username === prev ? 'selected' : '';
    html += `<option value="${esc(acc.username)}" ${selAttr}>@${esc(acc.username)}</option>`;
  });
  sel.innerHTML = html;
  // keep pagination info updated
  updateKernelsPaginationUI();
  refreshIcons();
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

async function loadKernelsForAccount() {
  const sel = document.getElementById('kernels-account-select');
  const searchEl = document.getElementById('kernels-search');
  const tbody = document.getElementById('kernels-table-body');
  const countEl = document.getElementById('kernels-count');
  if (!sel || !tbody) return;

  const account = sel.value.trim();
  if (!account) {
    showToast('Select an account first', 'warning');
    return;
  }
  kernelsState.account = account;
  kernelsState.search = (searchEl ? searchEl.value.trim() : "");
  // if account changed, reset page
  // keep current page for pagination

  tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-slate-400"><i data-lucide="loader-2" class="w-5 h-5 animate-spin mx-auto mb-2 text-indigo-400"></i> Listing notebooks for @${esc(account)}...</td></tr>`;
  refreshIcons();
  if (countEl) countEl.textContent = '';

  const nextBtn = document.getElementById('btn-kernels-next');
  if (nextBtn) nextBtn.disabled = true;

  try {
    const params = new URLSearchParams({ account, search: kernelsState.search, page: String(kernelsState.page), pageSize: String(kernelsState.pageSize) });
    const res = await fetch(`/api/kernels/list?${params.toString()}`);
    if (res.status === 401) { window.location.href = '/login'; return; }
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'Failed to list kernels');

    kernelsState.kernels = data.kernels || [];
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
            <button onclick="openKernelsDetail('${esc(kernelsState.account)}','${esc(ref)}','${esc(title.replace(/'/g, "\\'"))}')" class="px-2.5 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600/20 text-indigo-300 hover:bg-indigo-600/30 border border-indigo-500/30 transition">Open</button>
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
  // Auto-refresh files and logs preview
  kernelsListFiles();
  document.getElementById('kernels-logs-pre').textContent = 'Fetching logs...';
  kernelsFetchLogs();
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
  if (tbody) tbody.innerHTML = `<tr><td colspan="3" class="px-3 py-6 text-center text-slate-400"><i data-lucide="loader-2" class="w-4 h-4 animate-spin mx-auto mb-1"></i> Listing files...</td></tr>`;
  refreshIcons();
  try {
    const params = new URLSearchParams({ account, kernel_ref: ref });
    const res = await fetch(`/api/kernels/files?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'failed');
    renderKernelsFiles(data);
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="3" class="px-3 py-6 text-center text-rose-400">${esc(err.message)}</td></tr>`;
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
  tbody.innerHTML = rows.map(f => {
    const name = f.name || f.fileName || f.rel_path;
    const size = f.size != null ? (typeof f.size === 'number' ? formatBytes(f.size) : esc(String(f.size))) : '—';
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

async function kernelsPullOutput() {
  if (!kernelsState.selected) return;
  const { account, ref } = kernelsState.selected;
  const btn = document.getElementById('btn-kernels-pull');
  if (btn) btn.disabled = true;
  showToast(`Pulling outputs for ${ref}...`, 'info');
  try {
    const res = await fetch('/api/kernels/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_username: account, kernel_ref: ref })
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || data.error || 'pull failed');
    showToast(data.message || 'Pulled', 'success');
    kernelsListFiles();
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function kernelsFetchLogs() {
  if (!kernelsState.selected) return;
  const { account, ref } = kernelsState.selected;
  const pre = document.getElementById('kernels-logs-pre');
  if (pre) pre.textContent = 'Fetching logs from Kaggle...';
  try {
    const params = new URLSearchParams({ account, kernel_ref: ref });
    const res = await fetch(`/api/kernels/logs?${params.toString()}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.detail || 'failed');
    const logs = data.logs || '';
    if (pre) pre.textContent = logs ? logs : '(no logs returned)';
  } catch (err) {
    if (pre) pre.textContent = 'Error: ' + err.message;
  }
}

function kernelsDownloadZip() {
  if (!kernelsState.selected) { showToast('Open a kernel first','warning'); return; }
  const { account, ref } = kernelsState.selected;
  const params = new URLSearchParams({ account, kernel_ref: ref });
  window.open(`/api/kernels/files/download-zip?${params.toString()}`, '_blank');
}
