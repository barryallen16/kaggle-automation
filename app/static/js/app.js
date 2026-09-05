// Kaggle Multi-Account Automation Platform - Core App Module
const AppState = {
  activeTab: 'dashboard',
  accounts: [],
  activeRuns: [],
  allRuns: [],
  selectedTerminalRunId: null,
  selectedFilesRunId: null,
  cartFiles: [], // merge cart: [{run_id, filename, label}] across finished notebooks
  // Long-running server operations (fetched from /api/ops/status). Buttons
  // stay disabled/"Stopping..." while the matching op is in flight so a page
  // refresh mid-operation never lets the user re-trigger Stop All, launch
  // another distribute batch, etc.
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

// Optimistically mark a run/workload as stopping so renders flip to the busy
// button before the next server poll confirms it.
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

// Busy-stop button markup shared by dashboard/history rows.
function stoppingRunButtonHtml(runId, cls) {
  if (isRunStopping(runId)) {
    return `<button type="button" disabled class="${cls} opacity-60 cursor-not-allowed"><i data-lucide="loader-2" class="w-3.5 h-3.5 animate-spin"></i><span>Stopping...</span></button>`;
  }
  return `<button type="button" onclick="stopRun('${esc(runId)}')" class="${cls}"><span>Stop</span></button>`;
}

// Apply ops state to static (non-row) buttons: distribute + single-run launch
// + top-bar quota refresh. Row buttons are handled by their render functions.
function syncOpsStaticButtons() {
  const distBtn = document.getElementById('btn-launch-dist');
  if (distBtn) setButtonBusy(distBtn, !!AppState.ops?.distributing, 'Deploying Shards to Accounts...');

  const runBtn = document.getElementById('btn-launch-run');
  if (runBtn) setButtonBusy(runBtn, !!AppState.ops?.single_launching, 'Deploying to Kaggle CLI...');

  const refreshBtn = document.getElementById('btn-refresh-quotas');
  if (refreshBtn) setButtonBusy(refreshBtn, !!AppState.ops?.refreshing_quotas, 'Refreshing Quotas...');
}

// Generic busy toggler that remembers + restores the original button HTML.
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

// Fetch the authoritative ops snapshot (e.g. right after an operation we
// started finishes) so stale poll data can't keep a button busy.
async function refreshOps() {
  try {
    const opsRes = await fetchAuthedJson('/api/ops/status');
    if (opsRes && opsRes.success) AppState.ops = Object.assign({}, AppState.ops, opsRes);
  } catch (_) {}
  syncOpsStaticButtons();
}

// HTML escaping helper - ALWAYS use for user-controlled data inserted into innerHTML
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Account quota helper - returns remaining GPU and TPU hours
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

// Sort accounts descending: highest GPU quota left first, then TPU quota left, then username
function sortAccountsDescending(accounts) {
  return [...accounts].sort((a, b) => {
    const qA = getAccountRemainingQuota(a);
    const qB = getAccountRemainingQuota(b);
    if (qB.gpuLeft !== qA.gpuLeft) return qB.gpuLeft - qA.gpuLeft;
    if (qB.tpuLeft !== qA.tpuLeft) return qB.tpuLeft - qA.tpuLeft;
    return (a.username || '').localeCompare(b.username || '');
  });
}

// Toast notification helper
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
    <span class="text-xs font-semibold"></span>
  `;
  // Set user-controlled text safely (never via innerHTML)
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

// Mobile sidebar drawer: off-canvas below lg, static column from lg up.
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

// Desktop sidebar collapse: full column (labels + quick stats) <-> narrow
// icon-only rail that gives the content more room. The choice is saved per
// browser so it survives reloads, and only applies on lg+ screens - the
// mobile drawer always opens fully expanded.
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

  // When collapsed, surface each destination as a tooltip from its label text
  sidebar.querySelectorAll('.nav-tab').forEach(btnEl => {
    const label = btnEl.querySelector(':scope > span');
    if (collapsed && label && label.textContent.trim()) {
      btnEl.title = label.textContent.trim();
    } else {
      btnEl.removeAttribute('title');
    }
  });
}

// Tab navigation router.
// Tabs are pushed onto browser history (/?tab=<id>) so Back/Forward move
// between app views instead of exiting to the stale /login entry - the whole
// app lives on a single page, and without this the back button left the site.
function switchTab(tabId, push = true) {
  AppState.activeTab = tabId;

  // Phone UX: navigating always closes the slide-in menu
  if (window.innerWidth < 1024) toggleSidebar(false);

  // Update nav buttons
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.remove('active');
  });
  const activeBtn = document.getElementById(`nav-${tabId}`);
  if (activeBtn) activeBtn.classList.add('active');

  // Hide all sections
  const tabs = ['dashboard', 'runner', 'distributed', 'terminal', 'files', 'history', 'kernels', 'settings'];
  tabs.forEach(t => {
    const sec = document.getElementById(`tab-${t}`);
    if (sec) sec.classList.add('hidden');
  });

  // Show active section
  const currentSec = document.getElementById(`tab-${tabId}`);
  if (currentSec) currentSec.classList.remove('hidden');

  // Update page title
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

  // Record the view in browser history (never for popstate-driven switches)
  if (push && window.history && window.history.pushState) {
    try {
      const url = tabId === 'dashboard' ? '/' : '/?tab=' + encodeURIComponent(tabId);
      window.history.pushState({ tab: tabId }, '', url);
    } catch (err) { /* file:// or sandboxed - ignore */ }
  }

  // Trigger tab-specific refresh
  if (tabId === 'dashboard') loadDashboardData();
  if (tabId === 'history') loadHistoryData();
  if (tabId === 'terminal') updateTerminalRunDropdown();
  if (tabId === 'files') { updateFilesRunDropdown(); renderMergeCart(); }
  if (tabId === 'runner' || tabId === 'distributed') populateAccountSelects();
  if (tabId === 'kernels') initKernelsTab();
  if (tabId === 'settings') loadSettingsData();

  // Apply in-flight op state immediately (launch/distribute/quota buttons)
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

// Engine indicator: reflect real CLI availability instead of a static label
function updateEngineIndicator(cliAvailable) {
  const el = document.getElementById('live-indicator');
  if (!el) return;
  if (cliAvailable === undefined || cliAvailable === null) return; // unknown - keep as-is
  if (cliAvailable) {
    el.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-950/80 text-emerald-400 border border-emerald-800/60';
    el.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 pulsing-dot"></span> CLI Engine Online';
  } else {
    el.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-rose-950/80 text-rose-400 border border-rose-800/60';
    el.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-rose-500 mr-1.5"></span> Kaggle CLI Not Found';
  }
}

// Fetch JSON; on an expired session bounce to the login page immediately.
async function fetchAuthedJson(url) {
  const res = await fetch(url);
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('session expired');
  }
  return res.json();
}

// Global data refresh
async function refreshGlobalData() {
  try {
    const [accRes, runsRes, healthRes, opsRes] = await Promise.all([
      fetchAuthedJson('/api/accounts'),
      fetchAuthedJson('/api/runs'),
      fetch('/api/health').then(r => r.json()).catch(() => null),
      fetchAuthedJson('/api/ops/status').catch(() => null)
    ]);

    updateEngineIndicator(healthRes ? healthRes.cli_available : false);

    if (opsRes && opsRes.success) {
      AppState.ops = Object.assign({}, AppState.ops, opsRes);
    }
    syncOpsStaticButtons();
    // Keep the distributed workload rows live while on that tab: a Stop All
    // (possibly started before a page refresh) must flip back to "Stop All"
    // the moment the server finishes, so reload on every poll tick.
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
      // Keep all account selectors in sync (runner, distributed, kernels)
      if (typeof populateAccountSelects === 'function') {
        try { populateAccountSelects(); } catch (_) {}
      }
      if (typeof populateKernelsAccountSelect === 'function') {
        try { populateKernelsAccountSelect(); } catch (_) {}
      }
      if (typeof renderDistributedAccountCheckboxes === 'function') {
        // Only re-render distributed checkboxes if user is on that tab or has not yet loaded them
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
      if (ktr) ktr.innerText = AppState.allRuns.length;
      // Keep dropdowns that depend on runs in sync
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

// Auto polling loop every 8 seconds
document.addEventListener('DOMContentLoaded', () => {
  // Deep-link: activate the tab from the URL (?tab=... or #...) if present
  const initialTab = tabFromLocation();
  if (initialTab && initialTab !== 'dashboard') {
    switchTab(initialTab, false);
  }

  // Back/Forward navigate between tabs without reloading the app
  window.addEventListener('popstate', () => {
    const tab = tabFromLocation() || 'dashboard';
    switchTab(tab, false);
  });

  // Restore the saved desktop sidebar state and react to viewport changes
  applySidebarCollapse();
  window.addEventListener('resize', applySidebarCollapse);

  refreshGlobalData();
  setInterval(refreshGlobalData, 8000);
});

// ------------------------------------------------------------------
// Vimium / keyboard-first dropdown support.
//
// Vimium-style extensions intercept Enter, Space and the arrow keys in
// normal mode, and their link-hint "clicks" cannot open a native <select>
// popup (a synthetic click is not a real user gesture). Net result: the
// dropdowns never open from the keyboard. Fix: while a single-value
// <select> is focused, our own keydown handler treats Enter/Space/arrows
// as "open this picker" and calls select.showPicker() - a genuine user
// gesture, so the browser opens the native popup. Once it is open the
// popup owns the keyboard (arrows + typeahead + Enter all work, Vimium
// keys no longer apply). Plain listboxes (size > 1) are left untouched.
// ------------------------------------------------------------------
function openSelectPicker(sel) {
  if (!sel || sel.disabled || !sel.options || sel.options.length === 0) return false;
  if (typeof sel.showPicker !== 'function') return false;
  try {
    sel.showPicker();
    return true;
  } catch (_) {
    return false; // not user-activated, or hidden/iframed - let native behavior run
  }
}

document.addEventListener('keydown', (e) => {
  const t = e.target;
  if (!t || t.tagName !== 'SELECT' || t.size > 1) return; // listbox: keep native arrows
  if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  if (!openSelectPicker(t)) return; // fall back to the browser's default handling
  // Picker opened: swallow the key so Vimium can't scroll (Space) or the
  // surrounding <form> isn't accidentally submitted (Enter on a select).
  e.preventDefault();
  e.stopPropagation();
}, true); // capture phase: run before Vimium's normal-mode handlers
