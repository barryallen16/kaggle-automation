// Single Run & Trial Launcher Module

let uploadedFileContent = null;
let uploadedFileName = "notebook.ipynb";

function populateAccountSelects() {
  const runnerSelect = document.getElementById('runner-account-select');
  if (runnerSelect) {
    runnerSelect.innerHTML = '<option value="">-- Select Target Account --</option>' +
      AppState.accounts.map(a => `<option value="${esc(a.username)}">@${esc(a.username)}</option>`).join('');
  }
}

function handleFileInputChange(e) {
  const file = e.target.files[0];
  if (!file) return;

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
  btn.disabled = true;
  btn.innerHTML = `<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>Deploying to Kaggle CLI...</span>`;
  lucide.createIcons();

  try {
    showToast(isTrial ? 'Initiating Trial Run on Kaggle...' : 'Deploying full notebook session to Kaggle...', 'info');
    
    const res = await fetch('/api/runs/launch-json', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (res.ok && data.success) {
      showToast('Kernel pushed and queued on Kaggle!', 'success');
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
    btn.disabled = false;
    btn.innerHTML = `<i data-lucide="rocket" class="w-4 h-4"></i><span>Deploy & Launch to Kaggle</span>`;
    lucide.createIcons();
  }
}
