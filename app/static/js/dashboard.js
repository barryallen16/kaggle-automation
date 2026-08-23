// Dashboard & Accounts Module

async function loadDashboardData() {
  await refreshGlobalData();
}

function renderDashboard() {
  const grid = document.getElementById('accounts-grid');
  if (!grid) return;

  if (AppState.accounts.length === 0) {
    grid.innerHTML = `
      <div class="col-span-full glass-panel rounded-2xl p-12 text-center border border-dashed border-slate-800">
        <i data-lucide="user-x" class="w-12 h-12 mx-auto mb-3 text-slate-600"></i>
        <h4 class="text-base font-bold text-slate-300">No Kaggle Accounts Configured</h4>
        <p class="text-xs text-slate-500 max-w-md mx-auto mt-1 mb-4">Add your Kaggle access tokens or API keys to start automating notebook runs and monitoring quotas.</p>
        <button onclick="openAddAccountModal()" class="inline-flex items-center space-x-2 px-4 py-2 rounded-xl text-xs font-bold bg-cyan-600 text-white hover:bg-cyan-500 transition">
          <i data-lucide="plus" class="w-4 h-4"></i>
          <span>Add Your First Account</span>
        </button>
      </div>
    `;
    lucide.createIcons();
    return;
  }

  grid.innerHTML = AppState.accounts.map(acc => {
    const quota = acc.last_quota || {};
    const gpu = quota.gpu || { used: 0, limit: 30, percent: 0, unit: 'hours' };
    const tpu = quota.tpu || { used: 0, limit: 20, percent: 0, unit: 'hours' };
    const activeRuns = acc.active_runs || [];

    const gpuPercent = Math.min(100, Math.max(0, gpu.percent || 0));
    const tpuPercent = Math.min(100, Math.max(0, tpu.percent || 0));

    const gpuRemaining = Math.max(0, (gpu.limit - gpu.used)).toFixed(1);
    const tpuRemaining = Math.max(0, (tpu.limit - tpu.used)).toFixed(1);

    const activeBadge = activeRuns.length > 0
      ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950/80 text-emerald-400 border border-emerald-800/60">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 pulsing-dot"></span> ${activeRuns.length} Running
        </span>`
      : `<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-800 text-slate-400">Idle</span>`;

    const activeList = activeRuns.map(r => `
      <div class="mt-2 p-2 rounded-lg bg-[#0a0d14] border border-[#1e293b] flex items-center justify-between text-xs">
        <div class="truncate max-w-[200px]">
          <p class="font-bold text-white truncate">${esc(r.title)}</p>
          <p class="text-[10px] text-slate-400 font-mono">${esc(r.accelerator)}</p>
        </div>
        <button onclick="stopRun('${esc(r.id)}')" class="px-2 py-1 rounded text-[10px] font-bold bg-rose-600/20 text-rose-400 hover:bg-rose-600/40 transition">
          Stop
        </button>
      </div>
    `).join('');

    return `
      <div class="glass-card rounded-2xl p-5 border border-[#222d4a] relative flex flex-col justify-between space-y-4">
        <!-- Account Header -->
        <div class="flex items-start justify-between">
          <div class="flex items-center space-x-3">
            <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/30 flex items-center justify-center font-bold text-cyan-400">
              ${esc(acc.username.charAt(0).toUpperCase())}
            </div>
            <div>
              <h4 class="text-sm font-bold text-white">@${esc(acc.username)}</h4>
              <p class="text-[10px] text-slate-500 font-mono">Key: ${esc(acc.api_key_masked)}</p>
            </div>
          </div>
          <div class="flex items-center space-x-2">
            ${activeBadge}
            <button onclick="deleteAccount('${esc(acc.username)}')" title="Delete Account" class="text-slate-600 hover:text-rose-400 transition p-1">
              <i data-lucide="trash" class="w-3.5 h-3.5"></i>
            </button>
          </div>
        </div>

        <!-- Quota Meters -->
        <div class="space-y-3 pt-2">
          <!-- GPU Quota -->
          <div>
            <div class="flex justify-between text-xs mb-1 font-medium">
              <span class="text-slate-400 flex items-center space-x-1">
                <i data-lucide="zap" class="w-3.5 h-3.5 text-cyan-400"></i>
                <span>Weekly GPU (T4 x 2)</span>
              </span>
              <span class="text-white font-mono text-[11px]">${gpuRemaining}h left / ${gpu.limit}h</span>
            </div>
            <div class="w-full bg-[#0a0d14] rounded-full h-2 overflow-hidden border border-slate-800">
              <div class="bg-gradient-to-r from-cyan-500 to-blue-500 h-2 rounded-full transition-all duration-500" style="width: ${gpuPercent}%"></div>
            </div>
          </div>

          <!-- TPU Quota -->
          <div>
            <div class="flex justify-between text-xs mb-1 font-medium">
              <span class="text-slate-400 flex items-center space-x-1">
                <i data-lucide="cpu" class="w-3.5 h-3.5 text-purple-400"></i>
                <span>Weekly TPU (v3-8)</span>
              </span>
              <span class="text-white font-mono text-[11px]">${tpuRemaining}h left / ${tpu.limit}h</span>
            </div>
            <div class="w-full bg-[#0a0d14] rounded-full h-2 overflow-hidden border border-slate-800">
              <div class="bg-gradient-to-r from-purple-500 to-indigo-500 h-2 rounded-full transition-all duration-500" style="width: ${tpuPercent}%"></div>
            </div>
          </div>
        </div>

        <!-- Active Running Sessions if any -->
        ${activeRuns.length > 0 ? `<div><p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Active Execution:</p>${activeList}</div>` : ''}

        <!-- Footer Actions -->
        <div class="pt-2 border-t border-[#1e293b] flex items-center justify-between text-xs">
          <button onclick="refreshSingleQuota('${esc(acc.username)}')" class="text-slate-400 hover:text-cyan-400 flex items-center space-x-1 transition text-[11px]">
            <i data-lucide="rotate-cw" class="w-3 h-3"></i>
            <span>Refresh Quota</span>
          </button>
          <a href="https://kaggle.com/${encodeURIComponent(acc.username)}" target="_blank" class="text-slate-400 hover:text-blue-400 flex items-center space-x-1 transition text-[11px]">
            <span>Profile</span>
            <i data-lucide="external-link" class="w-3 h-3"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  renderActiveRunsTable();
  lucide.createIcons();
}

function formatMaxRuntime(run) {
  // Trials have short timeouts; showing "/ 12h" for them was misleading
  const secs = run.timeout_seconds;
  if (!secs) return '12h';
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  return `${(secs / 3600).toFixed(secs % 3600 ? 1 : 0)}h`;
}

function renderActiveRunsTable() {
  const tbody = document.getElementById('active-runs-table-body');
  if (!tbody) return;

  if (AppState.activeRuns.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-6 py-8 text-center text-slate-500">No active notebook sessions currently running.</td></tr>`;
    return;
  }

  tbody.innerHTML = AppState.activeRuns.map(run => {
    const startDt = new Date(run.start_time);
    const now = new Date();
    const elapsedMinutes = Math.floor((now - startDt) / 60000);
    const elapsedHours = (elapsedMinutes / 60).toFixed(1);

    const trialBadge = run.is_trial ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-900/60 text-amber-300 border border-amber-700/50 mr-1.5">TRIAL</span>' : '';

    return `
      <tr class="hover:bg-slate-800/30 transition">
        <td class="px-6 py-4 font-medium text-white">@${esc(run.account_username)}</td>
        <td class="px-6 py-4">
          <div class="font-bold text-white flex items-center">${trialBadge} ${esc(run.title)}</div>
          <a href="${esc(run.kaggle_url)}" target="_blank" rel="noopener noreferrer" class="text-xs text-cyan-400 hover:underline flex items-center space-x-1 mt-0.5">
            <span>${esc(run.kernel_ref)}</span>
            <i data-lucide="external-link" class="w-3 h-3 inline"></i>
          </a>
        </td>
        <td class="px-6 py-4 font-mono text-xs text-purple-300">${esc(run.accelerator)}</td>
        <td class="px-6 py-4 font-mono text-xs text-amber-300">${elapsedHours}h / ${formatMaxRuntime(run)}</td>
        <td class="px-6 py-4">
          <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 pulsing-dot"></span> ${esc(run.status.toUpperCase())}
          </span>
        </td>
        <td class="px-6 py-4 text-right space-x-2">
          <button onclick="viewLogsForRun('${esc(run.id)}')" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-slate-200 hover:bg-slate-700 transition">
            Live Stream
          </button>
          <button onclick="stopRun('${esc(run.id)}')" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-rose-600/20 text-rose-400 hover:bg-rose-600/40 border border-rose-500/30 transition">
            Stop
          </button>
        </td>
      </tr>
    `;
  }).join('');
}

// Modal handling
function openAddAccountModal() {
  const modal = document.getElementById('modal-add-account');
  if (modal) modal.classList.remove('hidden');
}

function closeAddAccountModal() {
  const modal = document.getElementById('modal-add-account');
  if (modal) modal.classList.add('hidden');
}

async function handleAddAccountSubmit(e) {
  e.preventDefault();
  const apiKey = document.getElementById('modal-api-key').value;
  const username = document.getElementById('modal-username').value;

  try {
    showToast('Authenticating with Kaggle CLI...', 'info');
    const res = await fetch('/api/accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey, username: username || null })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Account @${data.account.username} added successfully!`, 'success');
      closeAddAccountModal();
      document.getElementById('modal-api-key').value = '';
      document.getElementById('modal-username').value = '';
      refreshGlobalData();
    } else {
      showToast(data.detail || 'Failed to add account', 'error');
    }
  } catch (err) {
    showToast('Error adding account: ' + err.message, 'error');
  }
}

async function refreshAllQuotas() {
  showToast('Refreshing all Kaggle quotas...', 'info');
  try {
    const res = await fetch('/api/accounts/refresh', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('All quotas refreshed successfully!', 'success');
      refreshGlobalData();
    }
  } catch (err) {
    showToast('Failed to refresh quotas', 'error');
  }
}

async function refreshSingleQuota(username) {
  try {
    showToast(`Refreshing quota for @${username}...`, 'info');
    const res = await fetch(`/api/accounts/${username}/refresh`, { method: 'POST' });
    if (res.ok) {
      showToast(`Quota updated for @${username}`, 'success');
      refreshGlobalData();
    }
  } catch (err) {
    showToast(`Failed to refresh @${username}`, 'error');
  }
}

async function deleteAccount(username) {
  if (!confirm(`Are you sure you want to remove account @${username}?`)) return;
  try {
    const res = await fetch(`/api/accounts/${username}`, { method: 'DELETE' });
    if (res.ok) {
      showToast(`Account @${username} removed`, 'success');
      refreshGlobalData();
    }
  } catch (err) {
    showToast('Failed to remove account', 'error');
  }
}

async function stopRun(runId) {
  if (!confirm('Are you sure you want to stop this running session on Kaggle?')) return;
  showToast('Sending stop signal to Kaggle session...', 'warning');
  try {
    const res = await fetch(`/api/runs/${runId}/stop`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('Kernel stop signal deployed successfully!', 'success');
      refreshGlobalData();
    } else {
      showToast(data.error || 'Failed to stop run', 'error');
    }
  } catch (err) {
    showToast('Error stopping run: ' + err.message, 'error');
  }
}
