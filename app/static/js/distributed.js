// Distributed Workload Runner Module

let distUploadedFileContent = null;
let distUploadedFileName = "notebook.ipynb";

function renderDistributedAccountCheckboxes() {
  const container = document.getElementById('dist-accounts-checkboxes');
  if (!container) return;

  if (AppState.accounts.length === 0) {
    container.innerHTML = `<div class="col-span-full text-xs text-slate-500">No Kaggle accounts added yet. Add accounts in the Dashboard tab first.</div>`;
    return;
  }

  container.innerHTML = AppState.accounts.map((acc, idx) => `
    <label class="flex items-center space-x-2.5 p-2.5 rounded-lg bg-[#080b12] border border-[#1e293b] hover:border-purple-500/50 cursor-pointer transition">
      <input type="checkbox" name="dist-acc" value="${esc(acc.username)}" checked onchange="updateShardsPreview()" class="w-4 h-4 text-purple-500 rounded bg-slate-900 border-slate-700 focus:ring-0">
      <div class="truncate">
        <span class="text-xs font-bold text-white block truncate">@${esc(acc.username)}</span>
        <span class="text-[10px] text-slate-400 font-mono">${acc.last_quota?.gpu?.limit ? acc.last_quota.gpu.limit - acc.last_quota.gpu.used + 'h GPU left' : 'Active'}</span>
      </div>
    </label>
  `).join('');

  updateShardsPreview();
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

  const numAccounts = selectedAccounts.length;
  const chunkSize = Math.floor(totalItems / numAccounts);
  const remainder = totalItems % numAccounts;

  let currentStart = 0;
  let html = `<div class="mb-2 text-purple-300 font-bold">Partitioning ${totalItems.toLocaleString()} units across ${numAccounts} accounts (~${chunkSize.toLocaleString()} units/shard):</div>`;

  selectedAccounts.forEach((acc, idx) => {
    const extra = idx < remainder ? 1 : 0;
    const currentChunk = chunkSize + extra;
    const currentEnd = currentStart + currentChunk;

    html += `
      <div class="flex items-center justify-between p-1.5 rounded bg-purple-950/40 border border-purple-900/40">
        <span><strong>Shard ${idx + 1}/${numAccounts}</strong> (@${esc(acc)}):</span>
        <span class="text-cyan-300 font-mono">[${currentStart.toLocaleString()} ➔ ${currentEnd.toLocaleString()}] (${currentChunk.toLocaleString()} items)</span>
      </div>
    `;
    currentStart = currentEnd;
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
    timeout_seconds: 43200
  };

  const btn = document.getElementById('btn-launch-dist');
  btn.disabled = true;
  btn.innerHTML = `<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>Deploying Shards to ${selectedAccounts.length} Accounts...</span>`;
  lucide.createIcons();

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
    lucide.createIcons();
  }
}

// Hook into tab activation
const originalSwitchTab = window.switchTab;
window.switchTab = function(tabId) {
  if (originalSwitchTab) originalSwitchTab(tabId);
  if (tabId === 'distributed') {
    renderDistributedAccountCheckboxes();
  }
};
