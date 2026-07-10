// Kaggle Multi-Account Automation Platform - Core App Module
const AppState = {
  activeTab: 'dashboard',
  accounts: [],
  activeRuns: [],
  allRuns: [],
  selectedTerminalRunId: null,
  selectedFilesRunId: null
};

// HTML escaping helper - ALWAYS use for user-controlled data inserted into innerHTML
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
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

// Tab navigation router.
// Tabs are pushed onto browser history (/?tab=<id>) so Back/Forward move
// between app views instead of exiting to the stale /login entry - the whole
// app lives on a single page, and without this the back button left the site.
function switchTab(tabId, push = true) {
  AppState.activeTab = tabId;
  
  // Update nav buttons
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.remove('active');
  });
  const activeBtn = document.getElementById(`nav-${tabId}`);
  if (activeBtn) activeBtn.classList.add('active');

  // Hide all sections
  const tabs = ['dashboard', 'runner', 'distributed', 'terminal', 'files', 'history', 'settings'];
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
  if (tabId === 'files') updateFilesRunDropdown();
  if (tabId === 'runner' || tabId === 'distributed') populateAccountSelects();
  if (tabId === 'settings') loadSettingsData();
}

const KNOWN_TABS = ['dashboard', 'runner', 'distributed', 'terminal', 'files', 'history', 'settings'];

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
    const [accRes, runsRes, healthRes] = await Promise.all([
      fetchAuthedJson('/api/accounts'),
      fetchAuthedJson('/api/runs'),
      fetch('/api/health').then(r => r.json()).catch(() => null)
    ]);

    updateEngineIndicator(healthRes ? healthRes.cli_available : false);

    if (accRes.success) {
      AppState.accounts = accRes.accounts || [];
      document.getElementById('sidebar-accounts-count').innerText = AppState.accounts.length;
      document.getElementById('kpi-accounts-total').innerText = AppState.accounts.length;
    }

    if (runsRes.success) {
      AppState.allRuns = runsRes.runs || [];
      AppState.activeRuns = AppState.allRuns.filter(r => r.status === 'queued' || r.status === 'running');
      document.getElementById('sidebar-active-count').innerText = AppState.activeRuns.length;
      document.getElementById('kpi-active-runs').innerText = AppState.activeRuns.length;
      document.getElementById('kpi-total-runs').innerText = AppState.allRuns.length;
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

  refreshGlobalData();
  setInterval(refreshGlobalData, 8000);
});
