const AppState = {
  activeTab: 'dashboard',
  accounts: [],
  activeRuns: [],
  allRuns: [],
  selectedTerminalRunId: null,
  selectedFilesRunId: null,
  cartFiles: [],
  ops: {
    stopping_run_ids: [],
    stopping_workload_ids: [],
    stopping_kernel_refs: [],
    distributing: false,
    single_launching: false,
    refreshing_quotas: false
  }
};

function isRunStopping(runId) {
  return (AppState.ops?.stopping_run_ids || []).includes(runId);
}

function isWorkloadStopping(workloadId) {
  return (AppState.ops?.stopping_workload_ids || []).includes(workloadId);
}

function isKernelStopping(account, ref) {
  return (AppState.ops?.stopping_kernel_refs || []).includes(`${account}/${ref}`);
}

function markRunStopping(runId, on) {
  const list = AppState.ops.stopping_run_ids;
  const i = list.indexOf(runId);
  if (on && i === -1) list.push(runId);
  if (!on && i !== -1) list.splice(i, 1);
}

function markWorkloadStopping(workloadId, on) {
  const list = AppState.ops.stopping_workload_ids;
  const i = list.indexOf(workloadId);
  if (on && i === -1) list.push(workloadId);
  if (!on && i !== -1) list.splice(i, 1);
}

function markKernelStopping(account, ref, on) {
  const list = AppState.ops.stopping_kernel_refs;
  const key = `${account}/${ref}`;
  const i = list.indexOf(key);
  if (on && i === -1) list.push(key);
  if (!on && i !== -1) list.splice(i, 1);
}

function stoppingRunButtonHtml(runId, cls) {
  if (isRunStopping(runId)) {
    return `<button type="button" disabled class="${cls} opacity-60 cursor-not-allowed"><i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin"></i><span>Stopping...</span></button>`;
  }
  return `<button type="button" onclick="stopRun('${esc(runId)}')" class="${cls}"><span>Stop</span></button>`;
}

function syncOpsStaticButtons() {
  const distBtn = document.getElementById('btn-launch-dist');
  if (distBtn) setButtonBusy(distBtn, !!AppState.ops?.distributing, 'Deploying Shards to Accounts...');

  const runBtn = document.getElementById('btn-launch-run');
  if (runBtn) setButtonBusy(runBtn, !!AppState.ops?.single_launching, 'Deploying to Kaggle CLI...');

  const refreshBtn = document.getElementById('btn-refresh-quotas');
  if (refreshBtn) setButtonBusy(refreshBtn, !!AppState.ops?.refreshing_quotas, 'Refreshing Quotas...');
}

function setButtonBusy(btn, busy, busyLabel) {
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>${esc(busyLabel || 'Working...')}</span>`;
  } else {
    btn.disabled = false;
    if (btn.dataset.origHtml) {
      btn.innerHTML = btn.dataset.origHtml;
      delete btn.dataset.origHtml;
    }
  }
  try { refreshIcons(); } catch (_) {}
}

async function refreshOps() {
  try {
    const opsRes = await fetchAuthedJson('/api/ops/status');
    if (opsRes && opsRes.success) AppState.ops = Object.assign({}, AppState.ops, opsRes);
  } catch (_) {}
  syncOpsStaticButtons();
}

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function getAccountRemainingQuota(acc) {
  const quota = acc?.last_quota || {};
  const gpu = quota.gpu || { used: 0, limit: 30 };
  const tpu = quota.tpu || { used: 0, limit: 20 };
  const gpuLimit = typeof gpu.limit === 'number' ? gpu.limit : (parseFloat(gpu.limit) || 30);
  const gpuUsed = typeof gpu.used === 'number' ? gpu.used : (parseFloat(gpu.used) || 0);
  const tpuLimit = typeof tpu.limit === 'number' ? tpu.limit : (parseFloat(tpu.limit) || 20);
  const tpuUsed = typeof tpu.used === 'number' ? tpu.used : (parseFloat(tpu.used) || 0);
  return {
    gpuLeft: Math.max(0, gpuLimit - gpuUsed),
    tpuLeft: Math.max(0, tpuLimit - tpuUsed)
  };
}

function quotaKindForAccelerator(accelerator) {
  const a = String(accelerator || '').toLowerCase();
  if (!a || a === 'none' || a === 'default' || a === 'cpu') return null;
  if (a.includes('tpu') || a.includes('v3-8') || a.includes('v5e') || a.includes('v6e')) return 'tpu';
  return 'gpu';
}

const MAX_GPU_SESSIONS = 2;

function totalGpuHours(runs) {
  return (runs || []).reduce((sum, r) => {
    if (quotaKindForAccelerator(r && r.accelerator) !== 'gpu') return sum;
    if (!r || !r.end_time) return sum;
    const h = (new Date(r.end_time).getTime() - new Date(r.start_time).getTime()) / 36e5;
    if (!Number.isFinite(h) || h <= 0) return sum;
    const cap = (Number(r.timeout_seconds) || 43200) / 3600;
    return sum + Math.min(h, cap);
  }, 0);
}

function gpuSessionsBusy(acc) {
  return (acc?.active_runs || []).filter(r => {
    const a = String(r?.accelerator || 'none').toLowerCase();
    return a !== 'none' && a !== 'default' && a !== 'cpu';
  }).length;
}

function gpuSessionsFree(acc) {
  return Math.max(0, MAX_GPU_SESSIONS - gpuSessionsBusy(acc));
}

function isStuckSessionRun(r) {
  return (r && r.status) === 'cancelling';
}
function splitActiveRuns(acc) {
  const runs = (acc && acc.active_runs) || [];
  return {
    live: runs.filter(r => !isStuckSessionRun(r)),
    stuck: runs.filter(isStuckSessionRun)
  };
}

function busySessions(acc) {
  return gpuSessionsBusy(acc);
}

const QuotaAdapter = {
  MAX_SESSIONS: MAX_GPU_SESSIONS,
  remaining: getAccountRemainingQuota,
  kindFor: quotaKindForAccelerator,
  busy: busySessions,
  free: gpuSessionsFree,
};

function busyForAccount(username) {
  const info = (AppState.accounts || []).find(a => a.username === username);
  if (!info) return { busy: 0 };
  return { busy: gpuSessionsBusy(info) };
}

function sortAccountsDescending(accounts) {
  return [...accounts].sort((a, b) => {
    const qA = getAccountRemainingQuota(a);
    const qB = getAccountRemainingQuota(b);
    if (qB.gpuLeft !== qA.gpuLeft) return qB.gpuLeft - qA.gpuLeft;
    if (qB.tpuLeft !== qA.tpuLeft) return qB.tpuLeft - qA.tpuLeft;
    return (a.username || '').localeCompare(b.username || '');
  });
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  const bgColors = {
    success: 'bg-emerald-950/90 border-emerald-500 text-emerald-300',
    error: 'bg-rose-950/90 border-rose-500 text-rose-300',
    info: 'bg-cyan-950/90 border-cyan-500 text-cyan-300',
    warning: 'bg-amber-950/90 border-amber-500 text-amber-300'
  };

  const icons = {
    success: 'check-circle-2',
    error: 'alert-circle',
    info: 'info',
    warning: 'alert-triangle'
  };

  toast.className = `pointer-events-auto flex items-center space-x-3 px-4 py-3 rounded-xl border shadow-xl backdrop-blur-md transition-all duration-300 translate-x-5 opacity-0 ${bgColors[type] || bgColors.info}`;
  toast.innerHTML = `
    <i data-lucide="${icons[type] || 'info'}" class="w-4 h-4 flex-shrink-0"></i>
    <span class="text-xs font-semibold whitespace-pre-line break-words"></span>
  `;
    toast.querySelector('span').textContent = message;

  container.appendChild(toast);
  refreshIcons();

  setTimeout(() => {
    toast.classList.remove('translate-x-5', 'opacity-0');
  }, 10);

  setTimeout(() => {
    toast.classList.add('translate-x-5', 'opacity-0');
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

function toggleSidebar(force) {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (!sidebar) return;
  const shouldOpen = force !== undefined
    ? force
    : sidebar.classList.contains('-translate-x-full');
  sidebar.classList.toggle('-translate-x-full', !shouldOpen);
  if (overlay) overlay.classList.toggle('hidden', !shouldOpen);
}

function toggleSidebarCollapse() {
  const collapsed = localStorage.getItem('sidebarCollapsed') === '1';
  localStorage.setItem('sidebarCollapsed', collapsed ? '0' : '1');
  applySidebarCollapse();
}

function applySidebarCollapse() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const collapsed = window.innerWidth >= 1024 && localStorage.getItem('sidebarCollapsed') === '1';
  sidebar.classList.toggle('sidebar-collapsed', collapsed);

  const btn = document.getElementById('btn-sidebar-collapse');
  const iconCollapse = document.getElementById('icon-sidebar-collapse');
  const iconExpand = document.getElementById('icon-sidebar-expand');
  if (btn) {
    const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    btn.title = label;
    btn.setAttribute('aria-label', label);
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }
  if (iconCollapse) iconCollapse.classList.toggle('hidden', collapsed);
  if (iconExpand) iconExpand.classList.toggle('hidden', !collapsed);

    sidebar.querySelectorAll('.nav-tab').forEach(btnEl => {
    const label = btnEl.querySelector(':scope > span');
    if (collapsed && label && label.textContent.trim()) {
      btnEl.title = label.textContent.trim();
    } else {
      btnEl.removeAttribute('title');
    }
  });
}

function switchTab(tabId, push = true) {
  AppState.activeTab = tabId;

    if (window.innerWidth < 1024) toggleSidebar(false);

    document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.remove('active');
  });
  const activeBtn = document.getElementById(`nav-${tabId}`);
  if (activeBtn) activeBtn.classList.add('active');

    const tabs = ['dashboard', 'runner', 'distributed', 'terminal', 'files', 'history', 'kernels', 'settings'];
  tabs.forEach(t => {
    const sec = document.getElementById(`tab-${t}`);
    if (sec) sec.classList.add('hidden');
  });

    const currentSec = document.getElementById(`tab-${tabId}`);
  if (currentSec) currentSec.classList.remove('hidden');

    const titles = {
    dashboard: 'Multi-Account Dashboard',
    runner: 'Single Notebook Execution & Trial',
    distributed: 'Distributed Workload Sharder',
    terminal: 'Live Output & Log Console',
    files: 'Output Artifacts & File Downloader',
    history: 'Run Execution Catalog',
    kernels: 'Account Kernels Explorer',
    settings: 'Telegram Bot & System Settings'
  };
  const titleEl = document.getElementById('page-title');
  if (titleEl) titleEl.innerText = titles[tabId] || 'Dashboard';

    if (push && window.history && window.history.pushState) {
    try {
      const url = tabId === 'dashboard' ? '/' : '/?tab=' + encodeURIComponent(tabId);
      window.history.pushState({ tab: tabId }, '', url);
    } catch (err) {  }
  }

    if (tabId === 'dashboard') loadDashboardData();
  if (tabId === 'history') loadHistoryData();
  if (tabId === 'terminal') updateTerminalRunDropdown();
  if (tabId === 'files') { updateFilesRunDropdown(); renderMergeCart(); }
  if (tabId === 'runner' || tabId === 'distributed') populateAccountSelects();
  if (tabId === 'kernels') initKernelsTab();
  if (tabId === 'settings') loadSettingsData();

    syncOpsStaticButtons();
}

const KNOWN_TABS = ['dashboard', 'runner', 'distributed', 'terminal', 'files', 'history', 'kernels', 'settings'];

function tabFromLocation() {
  const fromQuery = new URLSearchParams(window.location.search).get('tab');
  if (fromQuery && KNOWN_TABS.includes(fromQuery)) return fromQuery;
  const hash = window.location.hash.replace('#', '');
  if (hash && KNOWN_TABS.includes(hash)) return hash;
  return null;
}

function updateEngineIndicator(cliAvailable) {
  const el = document.getElementById('live-indicator');
  if (!el) return;
  if (cliAvailable === undefined || cliAvailable === null) return;
  if (cliAvailable) {
    el.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-950/80 text-emerald-400 border border-emerald-800/60';
    el.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 pulsing-dot"></span> CLI Engine Online';
  } else {
    el.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-rose-950/80 text-rose-400 border border-rose-800/60';
    el.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-rose-500 mr-1.5"></span> Kaggle CLI Not Found';
  }
}

async function fetchAuthedJson(url) {
  const res = await fetch(url);
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('session expired');
  }
  return res.json();
}

async function refreshGlobalData() {
  try {
    const [accRes, runsRes, healthRes, opsRes] = await Promise.all([
      fetchAuthedJson('/api/accounts'),
      fetchAuthedJson('/api/runs?limit=500'),
      fetch('/api/health').then(r => r.json()).catch(() => null),
      fetchAuthedJson('/api/ops/status').catch(() => null)
    ]);

    updateEngineIndicator(healthRes ? healthRes.cli_available : false);

    if (opsRes && opsRes.success) {
      AppState.ops = Object.assign({}, AppState.ops, opsRes);
    }
    syncOpsStaticButtons();
                if (AppState.activeTab === 'distributed' && typeof loadWorkloadsData === 'function') {
      if (!window.__workloadsReloading) {
        window.__workloadsReloading = true;
        loadWorkloadsData().finally(() => { window.__workloadsReloading = false; });
      }
    }

    if (accRes.success) {
      AppState.accounts = sortAccountsDescending(accRes.accounts || []);
      const sa = document.getElementById('sidebar-accounts-count');
      if (sa) sa.innerText = AppState.accounts.length;
      const kpi = document.getElementById('kpi-accounts-total');
      if (kpi) kpi.innerText = AppState.accounts.length;
            if (typeof populateAccountSelects === 'function') {
        try { populateAccountSelects(); } catch (e) { console.error('populateAccountSelects failed', e); }
      }
      if (typeof populateKernelsAccountSelect === 'function') {
        try { populateKernelsAccountSelect(); } catch (_) {}
      }
      if (typeof renderDistributedAccountCheckboxes === 'function') {
                const distContainer = document.getElementById('dist-accounts-checkboxes');
        if (distContainer && (AppState.activeTab === 'distributed' || !distContainer.innerHTML.trim())) {
          try { renderDistributedAccountCheckboxes(); } catch (_) {}
        }
      }
    }

    if (runsRes.success) {
      AppState.allRuns = runsRes.runs || [];
      AppState.activeRuns = AppState.allRuns.filter(r => r.status === 'queued' || r.status === 'running');
      const sac = document.getElementById('sidebar-active-count');
      if (sac) sac.innerText = AppState.activeRuns.length;
      const kar = document.getElementById('kpi-active-runs');
      if (kar) kar.innerText = AppState.activeRuns.length;
      const ktr = document.getElementById('kpi-total-runs');
      if (ktr) ktr.innerText = runsRes.total ?? AppState.allRuns.length;
      const gpuH = typeof runsRes.total_gpu_hours === 'number'
        ? runsRes.total_gpu_hours
        : totalGpuHours(AppState.allRuns);
      const kgh = document.getElementById('kpi-gpu-hours');
      if (kgh) kgh.innerText = `${gpuH.toFixed(1)} GPU-hours`;
      const ksw = document.getElementById('kpi-single-weeks');
      if (ksw) ksw.innerText = `≈ ${(gpuH / 30).toFixed(1)} wks on 1 acct`;
      const ksub = document.getElementById('kpi-runs-sub');
      if (ksub) ksub.innerText = AppState.activeRuns.length ? `Logged in catalog · incl. ${AppState.activeRuns.length} active` : 'Logged in catalog';
            if (typeof updateTerminalRunDropdown === 'function' && AppState.activeTab === 'terminal') {
        try { updateTerminalRunDropdown(); } catch (_) {}
      }
      if (typeof updateFilesRunDropdown === 'function' && AppState.activeTab === 'files') {
        try { updateFilesRunDropdown(); } catch (_) {}
      }
    }

    if (AppState.activeTab === 'dashboard') renderDashboard();
    if (AppState.activeTab === 'history') renderHistory();
  } catch (err) {
    console.error('Error fetching global state:', err);
  }
}

document.addEventListener('DOMContentLoaded', () => {
    const initialTab = tabFromLocation();
  if (initialTab && initialTab !== 'dashboard') {
    switchTab(initialTab, false);
  }

    window.addEventListener('popstate', () => {
    const tab = tabFromLocation() || 'dashboard';
    switchTab(tab, false);
  });

    applySidebarCollapse();
  window.addEventListener('resize', applySidebarCollapse);

  refreshGlobalData();
  setInterval(refreshGlobalData, 8000);
});

function openSelectPicker(sel) {
  if (!sel || sel.disabled || !sel.options || sel.options.length === 0) return false;
  if (typeof sel.showPicker !== 'function') return false;
  try {
    sel.showPicker();
    return true;
  } catch (_) {
    return false;
  }
}

document.addEventListener('keydown', (e) => {
  const t = e.target;
  if (!t || t.tagName !== 'SELECT' || t.size > 1) return;
  if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  if (!openSelectPicker(t)) return;
      e.preventDefault();
  e.stopPropagation();
}, true); 
