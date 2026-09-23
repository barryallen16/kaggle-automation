// Single Run & Trial Launcher Module

let uploadedFileContent = null;
let uploadedFileName = "notebook.ipynb";

function updateRunnerAccountQuotaCard() {
  const container = document.getElementById('runner-account-quota-card');
  if (!container) return;

  const runnerSelect = document.getElementById('runner-account-select');
  const selectedUsername = runnerSelect ? runnerSelect.value : null;

  if (!selectedUsername) {
    container.innerHTML = `
      <div class="p-3 rounded-xl bg-[#08080B] border border-[#1E1E24] text-xs text-slate-500 flex items-center space-x-2">
        <i data-lucide="info" class="w-4 h-4 text-slate-400 flex-shrink-0"></i>
        <span>Select an account above to view live quota balance and active sessions.</span>
      </div>
    `;
    refreshIcons();
    updateRunnerQuotaWarning();
    return;
  }

  const acc = AppState.accounts.find(a => a.username === selectedUsername);
  if (!acc) {
    container.innerHTML = '';
    updateRunnerQuotaWarning();
    return;
  }

  const quota = acc.last_quota || {};
  const gpu = quota.gpu || { used: 0, limit: 30, percent: 0, unit: 'hours' };
  const tpu = quota.tpu || { used: 0, limit: 20, percent: 0, unit: 'hours' };
  const activeRuns = acc.active_runs || [];

  const gpuPercent = Math.min(100, Math.max(0, gpu.percent || 0));
  const tpuPercent = Math.min(100, Math.max(0, tpu.percent || 0));
  const gpuRemainingNum = Math.max(0, (gpu.limit - gpu.used));
  const tpuRemainingNum = Math.max(0, (tpu.limit - tpu.used));
  const gpuRemaining = gpuRemainingNum.toFixed(1);
  const tpuRemaining = tpuRemainingNum.toFixed(1);

  const activeStatus = activeRuns.length > 0
    ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950/80 text-emerald-400 border border-emerald-800/60">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 pulsing-dot"></span>${activeRuns.length} Running Session${activeRuns.length > 1 ? 's' : ''}
       </span>`
    : `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-800 text-slate-400">Idle</span>`;

  container.innerHTML = `
    <div class="p-3 rounded-xl bg-[#08080B] border border-[#26262E] space-y-2.5">
      <div class="flex items-center justify-between text-xs">
        <div class="flex items-center space-x-2 min-w-0">
          <span class="font-bold text-white truncate">@${esc(acc.username)}</span>
          <span class="text-[10px] text-slate-500 font-mono">(${esc(acc.api_key_masked)})</span>
        </div>
        <div>${activeStatus}</div>
      </div>

      <div class="grid grid-cols-2 gap-2.5 pt-0.5">
        <!-- GPU Quota Balance -->
        <div class="bg-[#101015] p-2 rounded-lg border ${gpuRemainingNum <= 0 ? 'border-amber-800/70' : 'border-[#1E1E24]'}">
          <div class="flex justify-between text-[11px] mb-1">
            <span class="text-slate-400 flex items-center space-x-1">
              <i data-lucide="zap" class="w-3 h-3 text-cyan-400"></i>
              <span>GPU (T4 x 2)</span>
            </span>
            <span class="text-white font-mono font-bold">${gpuRemaining}h / ${gpu.limit}h</span>
          </div>
          <div class="w-full bg-[#050507] rounded-full h-1.5 overflow-hidden border border-slate-800">
            <div class="${gpuRemainingNum <= 0 ? 'bg-amber-500' : 'bg-cyan-400'} h-1.5 rounded-full transition-all duration-300" style="width: ${gpuRemainingNum <= 0 ? 100 : gpuPercent}%"></div>
          </div>
          ${gpuRemainingNum <= 0 ? '<div class="text-[10px] text-amber-400/90 font-semibold mt-1">Empty — CPU runs only</div>' : ''}
        </div>

        <!-- TPU Quota Balance -->
        <div class="bg-[#101015] p-2 rounded-lg border ${tpuRemainingNum <= 0 ? 'border-amber-800/70' : 'border-[#1E1E24]'}">
          <div class="flex justify-between text-[11px] mb-1">
            <span class="text-slate-400 flex items-center space-x-1">
              <i data-lucide="cpu" class="w-3 h-3 text-purple-400"></i>
              <span>TPU (v3-8)</span>
            </span>
            <span class="text-white font-mono font-bold">${tpuRemaining}h / ${tpu.limit}h</span>
          </div>
          <div class="w-full bg-[#050507] rounded-full h-1.5 overflow-hidden border border-slate-800">
            <div class="${tpuRemainingNum <= 0 ? 'bg-amber-500' : 'bg-purple-400'} h-1.5 rounded-full transition-all duration-300" style="width: ${tpuRemainingNum <= 0 ? 100 : tpuPercent}%"></div>
          </div>
          ${tpuRemainingNum <= 0 ? '<div class="text-[10px] text-amber-400/90 font-semibold mt-1">Empty — CPU runs only</div>' : ''}
        </div>
      </div>
    </div>
  `;
  refreshIcons();
  updateRunnerQuotaWarning();
}

// Warns when the selected accelerator burns a quota pool the chosen account
// has none of (a GPU run on an empty account stalls until force-stopped and
// loses its output). Advisory only - the launch stays available, since CPU
// runs need no quota and the operator may know something the dashboard doesn't.
function updateRunnerQuotaWarning() {
  const hint = document.getElementById('runner-quota-warn');
  if (!hint) return;
  const accName = document.getElementById('runner-account-select')?.value;
  const accelerator = document.getElementById('runner-accelerator-select')?.value;
  const kind = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.kindFor)
    ? QuotaAdapter.kindFor(accelerator)
    : (typeof quotaKindForAccelerator === 'function' ? quotaKindForAccelerator(accelerator) : null);
  const acc = (AppState.accounts || []).find(a => a.username === accName);
  if (!acc || !kind) {
    hint.classList.add('hidden');
    hint.textContent = '';
    return;
  }
  const remaining = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.remaining)
    ? QuotaAdapter.remaining(acc)
    : (typeof getAccountRemainingQuota === 'function' ? getAccountRemainingQuota(acc) : { gpuLeft: 1, tpuLeft: 1 });
  const free = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.free)
    ? QuotaAdapter.free(acc)
    : (typeof gpuSessionsFree === 'function' ? gpuSessionsFree(acc) : 1);
  const cap = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.MAX_SESSIONS) || MAX_GPU_SESSIONS || 2;
  if (kind && free < 1) {
    hint.textContent = `⚠ @${acc.username} has no free sessions (${cap}/${cap} busy) — stop a run or pick another account.`;
    hint.classList.remove('hidden');
    return;
  }
  const left = kind === 'tpu' ? remaining.tpuLeft : remaining.gpuLeft;
  if (left > 0) {
    hint.classList.add('hidden');
    hint.textContent = '';
    return;
  }
  const pool = kind === 'tpu' ? 'TPU' : 'GPU';
  hint.textContent = `⚠ @${acc.username} has 0h ${pool} quota left — a ${pool} run will stall and may be force-stopped with its output lost. Switch to CPU or pick another account.`;
  hint.classList.remove('hidden');
}

function populateAccountSelects() {
  const runnerSelect = document.getElementById('runner-account-select');
  if (!runnerSelect) return;

  // Preserve previously chosen account or recall saved preference
  const prevValue = runnerSelect.value || AppState.selectedRunnerAccount || localStorage.getItem('kaggle_last_runner_account');
  const accounts = AppState.accounts || [];

  if (!accounts.length) {
    runnerSelect.innerHTML = '<option value="" disabled selected>No accounts — add one on the Dashboard</option>';
    updateRunnerAccountQuotaCard();
    return;
  }

  // Build options with username and quota left; flag quota-empty accounts so
  // they read as CPU-only options instead of looking broken. Accounts with no
  // free session slot for the chosen accelerator are disabled outright (a
  // launch there cannot land) - except CPU, which needs no slot.
  const accSel = document.getElementById('runner-accelerator-select');
  const kindFor = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.kindFor)
    ? QuotaAdapter.kindFor
    : (typeof quotaKindForAccelerator === 'function' ? quotaKindForAccelerator : () => 'gpu');
  const remaining = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.remaining)
    ? QuotaAdapter.remaining
    : (typeof getAccountRemainingQuota === 'function' ? getAccountRemainingQuota : () => ({ gpuLeft: 0, tpuLeft: 0 }));
  const freeSlots = (typeof QuotaAdapter !== 'undefined' && QuotaAdapter.free)
    ? QuotaAdapter.free
    : (typeof gpuSessionsFree === 'function' ? gpuSessionsFree : () => 1);

  const runnerKind = kindFor(accSel?.value);
  const optionsHtml = '<option value="">-- Select Target Account --</option>' +
    accounts.map(a => {
      const q = remaining(a);
      const free = freeSlots(a);
      const capped = !!runnerKind && free < 1;
      const flags = [
        q.gpuLeft <= 0 ? 'GPU empty' : '',
        q.tpuLeft <= 0 ? 'TPU empty' : '',
        capped ? 'sessions full' : ''
      ].filter(Boolean).join(' · ');
      return `<option value="${esc(a.username)}"${capped ? ' disabled' : ''}>@${esc(a.username)} — ${q.gpuLeft.toFixed(1)}h GPU / ${q.tpuLeft.toFixed(1)}h TPU left${flags ? ` (${flags})` : ''}</option>`;
    }).join('');

  runnerSelect.innerHTML = optionsHtml;

  // Restore preserved value, or fallback to the healthiest account (first in sorted list)
  if (prevValue && accounts.some(a => a.username === prevValue)) {
    runnerSelect.value = prevValue;
    AppState.selectedRunnerAccount = prevValue;
  } else if (accounts.length > 0) {
    runnerSelect.value = accounts[0].username;
    AppState.selectedRunnerAccount = accounts[0].username;
  }

  // Update the live quota card
  updateRunnerAccountQuotaCard();
}

// Hook change listener on the runner select once DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  loadRunnerPresets();
  const runnerSelect = document.getElementById('runner-account-select');
  if (runnerSelect) {
    runnerSelect.addEventListener('change', (e) => {
      AppState.selectedRunnerAccount = e.target.value;
      if (e.target.value) {
        localStorage.setItem('kaggle_last_runner_account', e.target.value);
      }
      updateRunnerAccountQuotaCard();
    });
  }
});

async function loadRunnerPresets() {
  const sel = document.getElementById('runner-preset-select');
  if (!sel) return;
  try {
    const res = await fetch('/api/presets');
    const data = await res.json();
    if (!res.ok || !data.success) return;
    for (const p of data.presets) {
      if (!p.available) continue;
      const opt = document.createElement('option');
      opt.value = p.key;
      opt.textContent = p.recommended ? p.label + ' (recommended)' : p.label;
      sel.appendChild(opt);
    }
  } catch (err) {
    console.error('Error loading presets:', err);
  }
}

async function handlePresetChange() {
  const sel = document.getElementById('runner-preset-select');
  const key = sel ? sel.value : '';
  if (!key) return;
  const textarea = document.getElementById('runner-code-textarea');
  if (textarea && textarea.value.trim()) {
    if (!confirm('Replace the current code with the preset script?')) {
      sel.value = '';
      return;
    }
  }
  try {
    const res = await fetch('/api/presets/' + encodeURIComponent(key));
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast('Failed to load preset', 'error');
      return;
    }
    if (textarea) {
      textarea.value = data.code;
      uploadedFileContent = data.code;
      uploadedFileName = data.filename;
    }
    const titleInput = document.getElementById('runner-title');
    if (titleInput && !titleInput.value) titleInput.value = data.title;
    const accSel = document.getElementById('runner-accelerator-select');
    if (accSel && accSel.value !== data.accelerator) {
      accSel.value = data.accelerator;
      populateAccountSelects();
    }
    const label = document.getElementById('file-upload-label');
    if (label) label.innerHTML = `Loaded preset: <strong class="text-cyan-400 font-mono">${data.filename}</strong>`;
    showToast('Preset loaded: ' + data.filename, 'success');
  } catch (err) {
    showToast('Error loading preset: ' + err.message, 'error');
  }
}

function handleFileInputChange(e) {
  const file = e.target.files[0];
  if (!file) return;

  // A hand-picked file replaces any preset: reset the dropdown to blank so
  // the form never implies preset content is loaded.
  const presetSel = document.getElementById('runner-preset-select');
  if (presetSel) presetSel.value = '';

  uploadedFileName = file.name;
  const label = document.getElementById('file-upload-label');
  if (label) label.innerHTML = `Loaded: <strong class="text-cyan-400 font-mono">${file.name}</strong> (${(file.size / 1024).toFixed(1)} KB)`;

  const reader = new FileReader();
  reader.onload = (event) => {
    uploadedFileContent = event.target.result;
    const textarea = document.getElementById('runner-code-textarea');
    if (textarea) textarea.value = uploadedFileContent;
    
    // Auto-fill title if empty
    const titleInput = document.getElementById('runner-title');
    if (titleInput && !titleInput.value) {
      const baseName = file.name.replace(/\.[^/.]+$/, "").replace(/[-_]/g, " ");
      titleInput.value = baseName.charAt(0).toUpperCase() + baseName.slice(1);
    }
  };
  reader.readAsText(file);
}

function toggleTrialFields() {
  const isTrial = document.getElementById('runner-is-trial').checked;
  const container = document.getElementById('trial-timeout-container');
  if (container) {
    if (isTrial) {
      container.classList.remove('hidden');
    } else {
      container.classList.add('hidden');
    }
  }
}

async function handleSingleRunSubmit(e) {
  e.preventDefault();
  
  const account = document.getElementById('runner-account-select').value;
  if (!account) {
    showToast('Please select a target Kaggle account first', 'warning');
    return;
  }
  AppState.selectedRunnerAccount = account;
  localStorage.setItem('kaggle_last_runner_account', account);
  const title = document.getElementById('runner-title').value;
  const accelerator = document.getElementById('runner-accelerator-select').value;
  const enableInternet = document.getElementById('runner-internet').checked;
  const isTrial = document.getElementById('runner-is-trial').checked;
  const timeoutSeconds = isTrial ? parseInt(document.getElementById('runner-trial-timeout').value || '300') : 43200;
  
  let code = document.getElementById('runner-code-textarea').value;
  if (!code.trim()) {
    // Default mock script if user left it blank
    code = `import time\nimport sys\n\nprint("🚀 Kaggle Session Started Successfully!")\nprint(f"Python Version: {sys.version}")\nfor i in range(10):\n    print(f"Step {i+1}/10: Processing batch data...")\n    time.sleep(2)\n\nprint("✅ Execution Complete!")\n`;
    uploadedFileName = "main.py";
  }

  const payload = {
    account_username: account,
    title: title,
    code_content: code,
    filename: uploadedFileName,
    accelerator: accelerator,
    enable_internet: enableInternet,
    is_trial: isTrial,
    timeout_seconds: timeoutSeconds
  };

  const btn = document.getElementById('btn-launch-run');
  if (AppState.ops?.single_launching) {
    showToast('A kernel launch is already in progress - please wait.', 'warning');
    return;
  }
  setButtonBusy(btn, true, 'Deploying to Kaggle CLI...');

  try {
    showToast(isTrial ? 'Initiating Trial Run on Kaggle...' : 'Deploying full notebook session to Kaggle...', 'info');
    
    const res = await fetch('/api/runs/launch-json', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (res.ok && data.success) {
      showToast(data.renamed ? `Kernel pushed as "${data.title}" (requested title was running)!` : 'Kernel pushed and queued on Kaggle!', 'success');
      await refreshGlobalData();
      
      // Automatically switch to Live Terminal for this run
      setTimeout(() => {
        switchTab('terminal');
        viewLogsForRun(data.run_id);
      }, 500);
    } else {
      showToast(data.message || data.error || 'Failed to push kernel', 'error');
    }
  } catch (err) {
    showToast('Launch failed: ' + err.message, 'error');
  } finally {
    await refreshOps();
  }
}
