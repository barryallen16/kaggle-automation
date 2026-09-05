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
    <div class="flex items-center space-x-2.5 p-2.5 rounded-lg bg-[#080b12] border border-[#1e293b] hover:border-purple-500/50 transition">
      <label class="flex items-center space-x-2.5 flex-1 min-w-0 cursor-pointer">
        <input type="checkbox" name="dist-acc" value="${esc(acc.username)}" ${shouldChecked ? 'checked' : ''} onchange="updateShardsPreview()" class="w-4 h-4 text-purple-500 rounded bg-slate-900 border-slate-700 focus:ring-0 flex-shrink-0">
        <div class="truncate flex-1">
          <span class="text-xs font-bold text-white block truncate">@${esc(acc.username)}</span>
          <span class="text-[10px] text-slate-400 font-mono">${esc(quotaLabel)}</span>
        </div>
      </label>
      <select onchange="updateShardsPreview();" 
              class="dist-session-select bg-[#0d121f] border border-[#222d4a] rounded-md px-1.5 py-1 text-[10px] text-purple-300 focus:outline-none focus:border-purple-500 flex-shrink-0"
              title="GPU sessions for this account (overrides the global setting)"
              data-acc="${esc(acc.username)}">
        <option value="1" ${sessVal === '1' ? 'selected' : ''}>1x</option>
        <option value="2" ${sessVal === '2' ? 'selected' : ''}>2x</option>
      </select>
    </div>
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

let distShardingMode = 'auto'; // 'auto' | 'manual'
let manualShardsList = []; // { id: number, account: string, startIndex: number, endIndex: number }
let manualShardSeq = 0;

function setDistShardingMode(mode) {
  distShardingMode = mode;
  const autoBtn = document.getElementById('dist-mode-btn-auto');
  const manualBtn = document.getElementById('dist-mode-btn-manual');
  const autoPanel = document.getElementById('dist-auto-shards-panel');
  const manualPanel = document.getElementById('dist-manual-shards-panel');

  if (mode === 'manual') {
    if (autoBtn) {
      autoBtn.className = 'px-3.5 py-1.5 text-xs font-bold rounded-lg text-slate-400 hover:text-white transition-all';
    }
    if (manualBtn) {
      manualBtn.className = 'px-3.5 py-1.5 text-xs font-bold rounded-lg bg-purple-600 text-white transition-all';
    }
    if (autoPanel) autoPanel.classList.add('hidden');
    if (manualPanel) manualPanel.classList.remove('hidden');

    // If manual shards list is currently empty, prefill from auto partitions as sensible default (UX Law 13)
    if (manualShardsList.length === 0) {
      populateManualFromAuto(false);
    }
  } else {
    if (autoBtn) {
      autoBtn.className = 'px-3.5 py-1.5 text-xs font-bold rounded-lg bg-purple-600 text-white transition-all';
    }
    if (manualBtn) {
      manualBtn.className = 'px-3.5 py-1.5 text-xs font-bold rounded-lg text-slate-400 hover:text-white transition-all';
    }
    if (autoPanel) autoPanel.classList.remove('hidden');
    if (manualPanel) manualPanel.classList.add('hidden');
    updateShardsPreview();
  }
  refreshIcons();
}

function addManualShardRow(account, start, end) {
  manualShardSeq++;
  const defaultAcc = account || (AppState.accounts.length > 0 ? AppState.accounts[0].username : '');
  
  let defStart = start;
  let defEnd = end;
  if (defStart === undefined || defStart === null) {
    if (manualShardsList.length > 0) {
      defStart = manualShardsList[manualShardsList.length - 1].endIndex;
    } else {
      defStart = 0;
    }
  }
  if (defEnd === undefined || defEnd === null) {
    defEnd = defStart + 2500000;
  }

  manualShardsList.push({
    id: manualShardSeq,
    account: defaultAcc,
    startIndex: Math.max(0, parseInt(defStart, 10) || 0),
    endIndex: Math.max(0, parseInt(defEnd, 10) || 0)
  });

  renderManualShards();
}

function removeManualShardRow(id) {
  manualShardsList = manualShardsList.filter(r => r.id !== id);
  renderManualShards();
}

function clearManualShards() {
  manualShardsList = [];
  renderManualShards();
}

function populateManualFromAuto(notify = true) {
  const selectedAccounts = getSelectedDistributedAccounts();
  if (selectedAccounts.length === 0) {
    if (notify) showToast('Please select at least one Kaggle account above first', 'warning');
    return;
  }

  const totalItems = parseInt(document.getElementById('dist-total-items')?.value || '10000000', 10);
  const sessionsMap = getPerAccountSessions();

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
  selectedAccounts.forEach(acc => {
    const { busy } = isGpuAcc(acc);
    const chosen = sessionsMap[acc] || 1;
    let slots = Math.max(0, chosen - busy);
    for (let i = 0; i < slots; i++) runnerList.push(acc);
  });

  const R = runnerList.length;
  if (R === 0) {
    if (notify) showToast('No free GPU session slots available on selected accounts', 'error');
    return;
  }

  const chunkSize = Math.floor(totalItems / R);
  const remainder = totalItems % R;

  manualShardsList = [];
  let currentStart = 0;
  runnerList.forEach((acc, idx) => {
    manualShardSeq++;
    const extra = idx < remainder ? 1 : 0;
    const currentChunk = chunkSize + extra;
    const currentEnd = currentStart + currentChunk;
    manualShardsList.push({
      id: manualShardSeq,
      account: acc,
      startIndex: currentStart,
      endIndex: currentEnd
    });
    currentStart = currentEnd;
  });

  renderManualShards();
  if (notify) {
    showToast(`Generated ${manualShardsList.length} shards from current configuration`, 'info');
  }
}

function renderManualShards() {
  const container = document.getElementById('dist-manual-shards-container');
  if (!container) return;

  if (manualShardsList.length === 0) {
    container.innerHTML = `
      <div class="p-6 text-center border border-dashed border-[#222d4a] rounded-xl text-slate-500 text-xs">
        <i data-lucide="file" class="w-8 h-8 mx-auto mb-2 text-slate-600"></i>
        <p class="font-medium text-slate-400">No manual shards defined</p>
        <p class="text-[11px] text-slate-500 mt-1 mb-3">Click "+ Add Shard" or "Prefill from Auto" to configure custom workload splits.</p>
        <button type="button" onclick="populateManualFromAuto()" class="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-purple-600 text-white hover:bg-purple-500 transition">
          <i data-lucide="zap" class="w-3.5 h-3.5"></i>
          <span>Prefill from Auto</span>
        </button>
      </div>
    `;
    updateManualShardsSummary();
    refreshIcons();
    return;
  }

  container.innerHTML = manualShardsList.map((row, idx) => {
    const itemCount = Math.max(0, row.endIndex - row.startIndex);
    const isInvalid = row.startIndex >= row.endIndex;

    const accountOptions = AppState.accounts.map(a => {
      const q = typeof getAccountRemainingQuota === 'function' ? getAccountRemainingQuota(a) : {
        gpuLeft: Math.max(0, (a.last_quota?.gpu?.limit || 30) - (a.last_quota?.gpu?.used || 0)),
        tpuLeft: Math.max(0, (a.last_quota?.tpu?.limit || 20) - (a.last_quota?.tpu?.used || 0))
      };
      return `<option value="${esc(a.username)}" ${a.username === row.account ? 'selected' : ''}>@${esc(a.username)} (${q.gpuLeft.toFixed(1)}h GPU left)</option>`;
    }).join('');

    return `
      <div class="flex items-center gap-2 sm:gap-3 p-2.5 rounded-xl bg-[#0b101d] border ${isInvalid ? 'border-amber-600/60' : 'border-[#1e293b]'} hover:border-purple-600/50 transition flex-wrap sm:flex-nowrap" data-row-id="${row.id}">
        <!-- Shard Badge -->
        <span class="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-purple-950/80 text-purple-300 font-bold text-xs flex-shrink-0 border border-purple-800/60">
          #${idx + 1}
        </span>

        <!-- Account Selector -->
        <div class="w-full sm:w-56 flex-shrink-0">
          <select onchange="updateManualRowAccount(${row.id}, this.value)" class="w-full bg-[#080c16] border border-[#222d4a] rounded-lg px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-purple-500">
            ${accountOptions}
          </select>
        </div>

        <!-- Range Inputs -->
        <div class="flex items-center space-x-2 flex-1 min-w-[200px]">
          <div class="relative flex-1">
            <span class="absolute left-2.5 top-1.5 text-[10px] text-slate-500 uppercase font-mono pointer-events-none">Start</span>
            <input type="number" min="0" value="${row.startIndex}" oninput="updateManualRowRange(${row.id}, 'start', this.value)" class="w-full bg-[#080c16] border ${isInvalid ? 'border-amber-500' : 'border-[#222d4a]'} rounded-lg pl-12 pr-2.5 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-purple-500">
          </div>
          <span class="text-slate-500 text-xs flex-shrink-0 font-bold">➔</span>
          <div class="relative flex-1">
            <span class="absolute left-2.5 top-1.5 text-[10px] text-slate-500 uppercase font-mono pointer-events-none">End</span>
            <input type="number" min="0" value="${row.endIndex}" oninput="updateManualRowRange(${row.id}, 'end', this.value)" class="w-full bg-[#080c16] border ${isInvalid ? 'border-amber-500' : 'border-[#222d4a]'} rounded-lg pl-10 pr-2.5 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-purple-500">
          </div>
        </div>

        <!-- Items Badge -->
        <div class="text-right w-28 flex-shrink-0">
          <span class="text-xs font-mono font-bold ${isInvalid ? 'text-amber-400' : 'text-cyan-300'}">${isInvalid ? 'Invalid Range' : itemCount.toLocaleString() + ' items'}</span>
        </div>

        <!-- Remove Button -->
        <button type="button" onclick="removeManualShardRow(${row.id})" class="p-1.5 text-slate-500 hover:text-rose-400 hover:bg-rose-950/40 rounded-lg transition flex-shrink-0" title="Delete Shard #${idx + 1}">
          <i data-lucide="x" class="w-4 h-4"></i>
        </button>
      </div>
    `;
  }).join('');

  updateManualShardsSummary();
  refreshIcons();
}

function updateManualRowAccount(rowId, account) {
  const row = manualShardsList.find(r => r.id === rowId);
  if (row) {
    row.account = account;
  }
}

function updateManualRowRange(rowId, field, valStr) {
  const row = manualShardsList.find(r => r.id === rowId);
  if (!row) return;

  const val = parseInt(valStr, 10) || 0;
  if (field === 'start') {
    row.startIndex = Math.max(0, val);
  } else {
    row.endIndex = Math.max(0, val);
  }

  // Update item badge for this row immediately (Doherty Threshold <400ms)
  const rowEl = document.querySelector(`[data-row-id="${rowId}"]`);
  if (rowEl) {
    const isInvalid = row.startIndex >= row.endIndex;
    const badge = rowEl.querySelector('.font-mono.font-bold');
    if (badge) {
      badge.className = `text-xs font-mono font-bold ${isInvalid ? 'text-amber-400' : 'text-cyan-300'}`;
      badge.textContent = isInvalid ? 'Invalid Range' : (Math.max(0, row.endIndex - row.startIndex)).toLocaleString() + ' items';
    }
  }

  updateManualShardsSummary();
}

function updateManualShardsSummary() {
  const countEl = document.getElementById('dist-manual-count');
  const itemsEl = document.getElementById('dist-manual-total-items');
  const statusEl = document.getElementById('dist-manual-validation-status');
  if (!countEl || !itemsEl || !statusEl) return;

  countEl.textContent = manualShardsList.length;
  const totalSpanned = manualShardsList.reduce((sum, r) => sum + Math.max(0, r.endIndex - r.startIndex), 0);
  itemsEl.textContent = totalSpanned.toLocaleString() + ' units';

  if (manualShardsList.length === 0) {
    statusEl.innerHTML = `<span class="text-slate-500">No shards configured yet</span>`;
    return;
  }

  const hasInvalidRange = manualShardsList.some(r => r.startIndex >= r.endIndex);
  const hasMissingAccount = manualShardsList.some(r => !r.account);

  if (hasInvalidRange) {
    statusEl.innerHTML = `<span class="inline-flex items-center text-amber-400 font-semibold"><i data-lucide="alert-triangle" class="w-3.5 h-3.5 mr-1"></i>Start index must be less than end index</span>`;
  } else if (hasMissingAccount) {
    statusEl.innerHTML = `<span class="inline-flex items-center text-rose-400 font-semibold"><i data-lucide="alert-circle" class="w-3.5 h-3.5 mr-1"></i>Every shard must have an assigned account</span>`;
  } else {
    statusEl.innerHTML = `<span class="inline-flex items-center text-emerald-400 font-semibold"><i data-lucide="check-circle-2" class="w-3.5 h-3.5 mr-1"></i>All ${manualShardsList.length} manual shards valid</span>`;
  }
  refreshIcons();
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
  const baseTitle = document.getElementById('dist-title').value;
  const totalItems = parseInt(document.getElementById('dist-total-items')?.value || '10000000', 10);
  const accelerator = document.getElementById('dist-accelerator').value;
  const enableInternet = document.getElementById('dist-internet').checked;

  let code = document.getElementById('dist-code-textarea').value;
  if (!code.trim()) {
    code = `import time\nimport sys\n\n# Note: SHARD_ID, TOTAL_SHARDS, START_INDEX, END_INDEX are auto-injected above!\nprint(f"🚀 [WORKER] Running Shard {SHARD_ID+1} of {TOTAL_SHARDS}")\nprint(f"Range: [{START_INDEX} -> {END_INDEX}]")\n\n# Simulated heavy distributed computation\nfor idx in range(START_INDEX, min(START_INDEX + 50, END_INDEX)):\n    if idx % 10 == 0:\n        print(f"Processed item: {idx}")\n    time.sleep(0.1)\n\nprint("✅ Shard processing complete!")\n`;
    distUploadedFileName = "distributed_worker.py";
  }

  let payload;
  let targetAccountsCount = 0;

  if (distShardingMode === 'manual') {
    if (manualShardsList.length === 0) {
      showToast('Please configure at least one manual shard or switch to Auto Partition', 'warning');
      return;
    }
    const hasInvalid = manualShardsList.some(r => r.startIndex >= r.endIndex);
    if (hasInvalid) {
      showToast('Please fix invalid shard ranges: start index must be less than end index', 'error');
      return;
    }
    const hasMissingAccount = manualShardsList.some(r => !r.account);
    if (hasMissingAccount) {
      showToast('Please select a target Kaggle account for every shard', 'warning');
      return;
    }

    const manualShardsPayload = manualShardsList.map((r, idx) => ({
      shard_index: idx,
      account: r.account,
      start_index: r.startIndex,
      end_index: r.endIndex
    }));

    const participatingAccounts = Array.from(new Set(manualShardsList.map(r => r.account)));
    const totalManualUnits = manualShardsList.reduce((sum, r) => sum + (r.endIndex - r.startIndex), 0);
    targetAccountsCount = participatingAccounts.length;

    payload = {
      base_title: baseTitle,
      code_content: code,
      filename: distUploadedFileName,
      accounts: participatingAccounts,
      total_items: totalManualUnits,
      start_offset: 0,
      accelerator: accelerator,
      enable_internet: enableInternet,
      is_trial: false,
      sessions_per_account: getPerAccountSessions(),
      timeout_seconds: 43200,
      manual_shards: manualShardsPayload
    };
  } else {
    // Auto partition mode
    if (selectedAccounts.length === 0) {
      showToast('Select at least one Kaggle account for distribution', 'warning');
      return;
    }
    targetAccountsCount = selectedAccounts.length;

    payload = {
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
  }

  const btn = document.getElementById('btn-launch-dist');
  if (AppState.ops?.distributing) {
    showToast('A distributed launch is already in progress - please wait.', 'warning');
    return;
  }
  setButtonBusy(btn, true, `Deploying Shards to ${targetAccountsCount} Accounts...`);

  try {
    showToast(`Distributing workload across ${targetAccountsCount} Kaggle accounts...`, 'info');
    
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
    await refreshOps();
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
      const stopping = isWorkloadStopping(w.id);
      const stopBtn = activeShards > 0
        ? (stopping
            ? `<button type="button" disabled class="inline-flex items-center space-x-1 px-2.5 py-1 rounded text-xs font-semibold bg-rose-600/20 text-rose-400 border border-rose-500/30 transition opacity-60 cursor-not-allowed"><i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin"></i><span>Stopping...</span></button>`
            : `<button onclick="stopWorkload('${esc(w.id)}')" class="px-2.5 py-1 rounded text-xs font-semibold bg-rose-600/20 text-rose-400 hover:bg-rose-600/40 border border-rose-500/30 transition">Stop All</button>`)
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
  if (isWorkloadStopping(workloadId)) {
    showToast('This workload is already being stopped - please wait.', 'warning');
    return;
  }
  if (!confirm('Stop ALL active shards of this workload on Kaggle?')) return;
  markWorkloadStopping(workloadId, true);
  await loadWorkloadsData(); // flip the Stop All row to its busy state immediately
  showToast('Stopping all workload shards...', 'warning');
  try {
    const res = await fetch(`/api/distributed/${workloadId}/stop`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
    } else {
      showToast(data.detail || 'Failed to stop workload', 'error');
    }
  } catch (err) {
    showToast('Error stopping workload: ' + err.message, 'error');
  } finally {
    markWorkloadStopping(workloadId, false);
    await refreshGlobalData();
    await loadWorkloadsData();
  }
}
