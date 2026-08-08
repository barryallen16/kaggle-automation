// Distributed Workload Runner Module

let distUploadedFileContent = null;
let distUploadedFileName = "notebook.ipynb";

// Renders GPU hours without float noise (17.399999 -> "17.4", 30 -> "30")
function fmtHours(value) {
  const n = Math.max(0, Number(value) || 0);
  return (Math.round(n * 10) / 10).toLocaleString('en-US', { maximumFractionDigits: 1 });
}

function renderDistributedAccountCheckboxes() {
  const container = document.getElementById('dist-accounts-checkboxes');
  if (!container) return;

  if (AppState.accounts.length === 0) {
    container.innerHTML = `<div class="col-span-full text-xs text-slate-500">No Kaggle accounts added yet. Add accounts in the Dashboard tab first.</div>`;
    return;
  }

  // Preserve user selections across re-renders (refreshGlobalData polls every 8s and would otherwise reset to all checked)
  const prevChecked = new Set();
  container.querySelectorAll('input[name="dist-acc"]').forEach(cb => {
    if (cb.checked) prevChecked.add(cb.value);
    else prevChecked.delete(cb.value);
  });
  const isFirstRender = prevChecked.size === 0 && !container.querySelector('input[name="dist-acc"]');
  const prevSessions = {};
  container.querySelectorAll('.dist-session-select').forEach(sel => {
    prevSessions[sel.dataset.acc] = sel.value;
  });
  const globalSessions = document.getElementById('dist-sessions-per-account')?.value || '2';

  container.innerHTML = AppState.accounts.map((acc) => {
    const gpuQ = acc.last_quota?.gpu;
    const hasQuota = gpuQ && isFinite(gpuQ.limit) && Number(gpuQ.limit) > 0;
    const quotaLabel = hasQuota
      ? `${fmtHours(gpuQ.limit - gpuQ.used)}h GPU left`
      : 'Active';
    const wasChecked = prevChecked.has(acc.username);
    const shouldChecked = isFirstRender ? true : wasChecked;
    const prevSess = prevSessions[acc.username];
    const sessVal = prevSess ? prevSess : globalSessions;
    return `
    <label class="flex items-center space-x-2.5 p-2.5 rounded-lg bg-[#080b12] border border-[#1e293b] hover:border-purple-500/50 cursor-pointer transition">
      <input type="checkbox" name="dist-acc" value="${esc(acc.username)}" ${shouldChecked ? 'checked' : ''} onchange="updateShardsPreview()" class="w-4 h-4 text-purple-500 rounded bg-slate-900 border-slate-700 focus:ring-0">
      <div class="truncate flex-1">
        <span class="text-xs font-bold text-white block truncate">@${esc(acc.username)}</span>
        <span class="text-[10px] text-slate-400 font-mono">${esc(quotaLabel)}</span>
      </div>
      <select onchange="event.stopPropagation(); updateShardsPreview();" onclick="event.stopPropagation()"
              class="dist-session-select bg-[#0d121f] border border-[#222d4a] rounded-md px-1.5 py-1 text-[10px] text-purple-300 focus:outline-none focus:border-purple-500"
              title="GPU sessions for this account (overrides the global setting)"
              data-acc="${esc(acc.username)}">
        <option value="1" ${sessVal === '1' ? 'selected' : ''}>1x</option>
        <option value="2" ${sessVal === '2' ? 'selected' : ''}>2x</option>
      </select>
    </label>
  `;
  }).join('');

  updateShardsPreview();
}

// Global "GPU Sessions / Account" applies to every account row at once;
// individual rows can then be tweaked independently afterwards.
function applyGlobalSessionsToAll() {
  const globalVal = document.getElementById('dist-sessions-per-account')?.value || '2';
  document.querySelectorAll('.dist-session-select').forEach(sel => { sel.value = globalVal; });
}

function getPerAccountSessions() {
  const map = {};
  const checked = new Set(getSelectedDistributedAccounts());
  document.querySelectorAll('.dist-session-select').forEach(sel => {
    const acc = sel.dataset.acc;
    if (checked.has(acc)) map[acc] = parseInt(sel.value, 10) || 1;
  });
  return map;
}

function getSelectedDistributedAccounts() {
  const checkboxes = document.querySelectorAll('input[name="dist-acc"]:checked');
  return Array.from(checkboxes).map(cb => cb.value);
}

function updateShardsPreview() {
  const selectedAccounts = getSelectedDistributedAccounts();
  const totalItems = parseInt(document.getElementById('dist-total-items')?.value || '10000000', 10);
  const preview = document.getElementById('dist-shards-preview');
  if (!preview) return;

  if (selectedAccounts.length === 0) {
    preview.innerHTML = `<span class="text-amber-400">Please select at least 1 Kaggle account to partition workload.</span>`;
    return;
  }

  const sessionsMap = getPerAccountSessions();

  // Mirror of the server-side runner plan: each account gets min(chosen, free
  // GPU slots) runners. GPU = anything that isn't none/default/cpu.
  const isGpuAcc = (acc) => {
    const info = (AppState.accounts || []).find(a => a.username === acc);
    if (!info) return { busy: 0 };
    const gpuRuns = (info.active_runs || []).filter(r => {
      const a = (r.accelerator || 'none').toLowerCase();
      return a !== 'none' && a !== 'default' && a !== 'cpu';
    });
    return { busy: gpuRuns.length };
  };

  const runnerList = [];
  const perAccount = [];
  selectedAccounts.forEach(acc => {
    const { busy } = isGpuAcc(acc);
    const chosen = sessionsMap[acc] || 1;
    let slots = Math.max(0, chosen - busy);
    perAccount.push({ acc, chosen, busy, slots });
    for (let i = 0; i < slots; i++) runnerList.push(acc);
  });

  const R = runnerList.length;
  if (R === 0) {
    preview.innerHTML = `<span class="text-rose-400">No free GPU session slots on the selected accounts - stop existing runs first.</span>`;
    return;
  }

  const chunkSize = Math.floor(totalItems / R);
  const remainder = totalItems % R;

  let currentStart = 0;
  let html = `<div class="mb-2 text-purple-300 font-bold">${totalItems.toLocaleString()} units across ${R} runner${R > 1 ? 's' : ''}:</div>`;

  perAccount.forEach(p => {
    const reduced = p.slots < p.chosen;
    html += `
      <div class="flex items-center justify-between text-[11px] px-1">
        <span>@${esc(p.acc)}: ${p.slots} runner${p.slots !== 1 ? 's' : ''} (${p.chosen}x requested)</span>
        <span class="${reduced ? 'text-amber-400' : 'text-purple-400/70'}">${reduced ? `capped - ${p.busy} GPU session(s) already active` : `${p.busy} active`}</span>
      </div>`;
  });

  let shardIdx = 0;
  perAccount.forEach(p => {
    for (let k = 0; k < p.slots; k++) {
      const extra = shardIdx < remainder ? 1 : 0;
      const currentChunk = chunkSize + extra;
      const currentEnd = currentStart + currentChunk;
      html += `
        <div class="flex items-center justify-between p-1.5 rounded bg-purple-950/40 border border-purple-900/40">
          <span><strong>Shard ${shardIdx + 1}/${R}</strong> (@${esc(p.acc)}):</span>
          <span class="text-cyan-300 font-mono">[${currentStart.toLocaleString()} ➔ ${currentEnd.toLocaleString()}] (${currentChunk.toLocaleString()} items)</span>
        </div>`;
      currentStart = currentEnd;
      shardIdx++;
    }
  });

  preview.innerHTML = html;
}

function handleDistFileInputChange(e) {
  const file = e.target.files[0];
  if (!file) return;

  distUploadedFileName = file.name;
  const label = document.getElementById('dist-file-upload-label');
  if (label) label.innerHTML = `Base Template: <strong class="text-purple-400 font-mono">${file.name}</strong> (${(file.size / 1024).toFixed(1)} KB)`;

  const reader = new FileReader();
  reader.onload = (event) => {
    distUploadedFileContent = event.target.result;
    const textarea = document.getElementById('dist-code-textarea');
    if (textarea) textarea.value = distUploadedFileContent;
    
    const titleInput = document.getElementById('dist-title');
    if (titleInput && !titleInput.value) {
      const baseName = file.name.replace(/\.[^/.]+$/, "").replace(/[-_]/g, " ");
      titleInput.value = "Distributed " + baseName.charAt(0).toUpperCase() + baseName.slice(1);
    }
  };
  reader.readAsText(file);
}

async function handleDistributedSubmit(e) {
  e.preventDefault();

  const selectedAccounts = getSelectedDistributedAccounts();
  if (selectedAccounts.length === 0) {
    showToast('Select at least one Kaggle account for distribution', 'warning');
    return;
  }

  const baseTitle = document.getElementById('dist-title').value;
  const totalItems = parseInt(document.getElementById('dist-total-items').value || '10000000', 10);
  const accelerator = document.getElementById('dist-accelerator').value;
  const enableInternet = document.getElementById('dist-internet').checked;

  let code = document.getElementById('dist-code-textarea').value;
  if (!code.trim()) {
    code = `import time\nimport sys\n\n# Note: SHARD_ID, TOTAL_SHARDS, START_INDEX, END_INDEX are auto-injected above!\nprint(f"🚀 [WORKER] Running Shard {SHARD_ID+1} of {TOTAL_SHARDS}")\nprint(f"Range: [{START_INDEX} -> {END_INDEX}]")\n\n# Simulated heavy distributed computation\nfor idx in range(START_INDEX, min(START_INDEX + 50, END_INDEX)):\n    if idx % 10 == 0:\n        print(f"Processed item: {idx}")\n    time.sleep(0.1)\n\nprint("✅ Shard processing complete!")\n`;
    distUploadedFileName = "distributed_worker.py";
  }

  const payload = {
    base_title: baseTitle,
    code_content: code,
    filename: distUploadedFileName,
    accounts: selectedAccounts,
    total_items: totalItems,
    start_offset: 0,
    accelerator: accelerator,
    enable_internet: enableInternet,
    is_trial: false,
    sessions_per_account: getPerAccountSessions(), // per-account overrides {username: 1|2}
    timeout_seconds: 43200
  };

  const btn = document.getElementById('btn-launch-dist');
  btn.disabled = true;
  btn.innerHTML = `<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>Deploying Shards to ${selectedAccounts.length} Accounts...</span>`;
  refreshIcons();

  try {
    showToast(`Distributing workload across ${selectedAccounts.length} Kaggle accounts...`, 'info');
    
    const res = await fetch('/api/distributed/launch-json', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (res.ok && data.success) {
      showToast(`Workload successfully partitioned & launched across ${data.total_shards} accounts!`, 'success');
      await refreshGlobalData();
      
      setTimeout(() => {
        switchTab('history');
      }, 600);
    } else {
      showToast(data.error || data.detail || 'Failed to dispatch distributed run', 'error');
    }
  } catch (err) {
    showToast('Distributed launch error: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<i data-lucide="cpu" class="w-4 h-4"></i><span>Distribute & Launch Across Accounts</span>`;
    refreshIcons();
  }
}

// Hook into tab activation
const originalSwitchTab = window.switchTab;
window.switchTab = function(tabId) {
  if (originalSwitchTab) originalSwitchTab(tabId);
  if (tabId === 'distributed') {
    renderDistributedAccountCheckboxes();
    loadWorkloadsData();
  }
};

function updateQuotaHint() {
  const hint = document.getElementById('dist-quota-hint');
  if (!hint) return;
  // Warn whenever ANY selected account is set to 2 sessions (global or override)
  const anyDouble = Object.values(getPerAccountSessions()).some(v => v >= 2);
  hint.classList.toggle('hidden', !anyDouble);
}

// ------------------------------------------------------------------
// Recent Workloads panel + Stop-Workload
// ------------------------------------------------------------------
async function loadWorkloadsData() {
  const tbody = document.getElementById('workloads-table-body');
  if (!tbody) return;
  try {
    const res = await fetch('/api/distributed');
    if (res.status === 401) { window.location.href = '/login'; return; }
    const data = await res.json();
    const workloads = data.workloads || [];

    if (workloads.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-6 text-center text-slate-500">No distributed workloads dispatched yet.</td></tr>`;
      return;
    }

    const badges = {
      running: 'bg-cyan-950 text-cyan-400 border-cyan-800',
      dispatched: 'bg-emerald-950 text-emerald-400 border-emerald-800',
      partial: 'bg-amber-950 text-amber-400 border-amber-800',
      failed: 'bg-rose-950 text-rose-400 border-rose-800',
      stopped: 'bg-slate-800 text-slate-400 border-slate-700'
    };

    tbody.innerHTML = workloads.map(w => {
      const shards = w.shards || [];
      const activeShards = shards.filter(s => s.status === 'queued' || s.status === 'running').length;
      const badge = badges[w.status] || 'bg-slate-800 text-slate-300 border-slate-700';
      const stopBtn = activeShards > 0
        ? `<button onclick="stopWorkload('${esc(w.id)}')" class="px-2.5 py-1 rounded text-xs font-semibold bg-rose-600/20 text-rose-400 hover:bg-rose-600/40 border border-rose-500/30 transition">Stop All</button>`
        : '';
      const accounts = Array.isArray(w.accounts_used)
        ? w.accounts_used.join(', ')
        : (w.accounts_used?.accounts || []).join(', ');
      return `
        <tr class="hover:bg-slate-800/30 transition">
          <td class="px-6 py-3.5">
            <div class="font-bold text-white">${esc(w.title)}</div>
            <div class="text-[10px] text-slate-500 font-mono mt-0.5">@ ${esc(accounts)} · ${shards.length} shard${shards.length !== 1 ? 's' : ''}</div>
          </td>
          <td class="px-6 py-3.5"><span class="px-2 py-0.5 rounded-full text-xs font-semibold border ${badge}">${esc(String(w.status).toUpperCase())}</span></td>
          <td class="px-6 py-3.5 font-mono text-xs text-slate-400">${shards.filter(s => s.status === 'complete').length}/${shards.length} done</td>
          <td class="px-6 py-3.5 text-xs text-amber-300 font-mono">${activeShards} active</td>
          <td class="px-6 py-3.5 text-right">${stopBtn}</td>
        </tr>`;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-6 text-center text-rose-400">Failed to load workloads: ${esc(err.message)}</td></tr>`;
  }
}

async function stopWorkload(workloadId) {
  if (!confirm('Stop ALL active shards of this workload on Kaggle?')) return;
  showToast('Stopping all workload shards...', 'warning');
  try {
    const res = await fetch(`/api/distributed/${workloadId}/stop`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
    } else {
      showToast(data.detail || 'Failed to stop workload', 'error');
    }
    refreshGlobalData();
    loadWorkloadsData();
  } catch (err) {
    showToast('Error stopping workload: ' + err.message, 'error');
  }
}
