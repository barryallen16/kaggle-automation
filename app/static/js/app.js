// Kaggle Multi-Account Automation Platform - Core App Module
const AppState = {
  activeTab: 'dashboard',
  accounts: [],
  activeRuns: [],
  allRuns: [],
  selectedTerminalRunId: null,
  selectedFilesRunId: null
};

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
    <span class="text-xs font-semibold">${message}</span>
  `;

  container.appendChild(toast);
  lucide.createIcons();

  setTimeout(() => {
    toast.classList.remove('translate-x-5', 'opacity-0');
  }, 10);

  setTimeout(() => {
    toast.classList.add('translate-x-5', 'opacity-0');
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

// Tab navigation router
function switchTab(tabId) {
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

  // Trigger tab-specific refresh
  if (tabId === 'dashboard') loadDashboardData();
  if (tabId === 'history') loadHistoryData();
  if (tabId === 'terminal') updateTerminalRunDropdown();
  if (tabId === 'files') updateFilesRunDropdown();
  if (tabId === 'runner' || tabId === 'distributed') populateAccountSelects();
  if (tabId === 'settings') loadSettingsData();
}

// Global data refresh
async function refreshGlobalData() {
  try {
    const [accRes, runsRes] = await Promise.all([
      fetch('/api/accounts').then(r => r.json()),
      fetch('/api/runs').then(r => r.json())
    ]);

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
  refreshGlobalData();
  setInterval(refreshGlobalData, 8000);
});
