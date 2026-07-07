// Live Logs & WebSocket Terminal Module

let terminalSocket = null;
let currentTerminalRunId = null;

function updateTerminalRunDropdown() {
  const select = document.getElementById('terminal-run-select');
  if (!select) return;

  const runs = AppState.allRuns || [];
  let html = '<option value="">-- Select a run to view output logs --</option>';
  
  runs.forEach(r => {
    const statusIcon = r.status === 'running' ? '🟢 ' : (r.status === 'complete' ? '✅ ' : (r.status === 'error' ? '❌ ' : '⏳ '));
    const selected = r.id === currentTerminalRunId ? 'selected' : '';
    const label = `${statusIcon} [${r.account_username}] ${r.title} (${r.status})`;
    html += `<option value="${esc(r.id)}" ${selected}>${esc(label)}</option>`;
  });

  select.innerHTML = html;
}

function handleTerminalRunSelect(e) {
  const runId = e.target.value;
  if (runId) {
    viewLogsForRun(runId);
  } else {
    disconnectTerminalSocket();
    document.getElementById('terminal-body').innerText = "Select a notebook execution from the dropdown above to stream output in real-time or view logs.";
    document.getElementById('terminal-stream-status').innerText = "Stream Disconnected";
    document.getElementById('terminal-run-info').innerText = "No Active Session";
  }
}

function viewLogsForRun(runId) {
  currentTerminalRunId = runId;
  AppState.selectedTerminalRunId = runId;

  const run = AppState.allRuns.find(r => r.id === runId);
  if (run) {
    document.getElementById('terminal-run-info').innerHTML = `Kernel: <strong class="text-cyan-400 font-mono">${esc(run.kernel_ref)}</strong> | Accelerator: <span class="text-purple-300">${esc(run.accelerator)}</span>`;
  }

  updateTerminalRunDropdown();
  connectTerminalSocket(runId);
}

function connectTerminalSocket(runId) {
  disconnectTerminalSocket();

  const terminalBody = document.getElementById('terminal-body');
  const statusEl = document.getElementById('terminal-stream-status');
  terminalBody.innerText = `Connecting live stream for run ${runId}...\n`;
  statusEl.innerText = "Connecting...";
  statusEl.className = "ml-2 font-mono text-[11px] text-amber-400";

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/runs/${runId}/logs`;

  terminalSocket = new WebSocket(wsUrl);

  terminalSocket.onopen = () => {
    statusEl.innerText = "● LIVE STREAM CONNECTED";
    statusEl.className = "ml-2 font-mono text-[11px] text-emerald-400";
  };

  terminalSocket.onmessage = (event) => {
    if (!event.data) return; // Keepalive empty string

    terminalBody.textContent += event.data;
    
    // Auto-scroll if enabled
    const autoscroll = document.getElementById('terminal-autoscroll')?.checked;
    if (autoscroll) {
      terminalBody.scrollTop = terminalBody.scrollHeight;
    }
  };

  terminalSocket.onclose = () => {
    statusEl.innerText = "Stream Offline / Finished";
    statusEl.className = "ml-2 font-mono text-[11px] text-slate-500";
  };

  terminalSocket.onerror = (err) => {
    statusEl.innerText = "Stream Error";
    statusEl.className = "ml-2 font-mono text-[11px] text-rose-400";
  };
}

function disconnectTerminalSocket() {
  if (terminalSocket) {
    terminalSocket.close();
    terminalSocket = null;
  }
}

async function fetchRemoteLogs() {
  if (!currentTerminalRunId) {
    showToast('Select a run first to fetch logs', 'warning');
    return;
  }

  showToast('Fetching complete logs directly from Kaggle CLI...', 'info');
  try {
    const res = await fetch(`/api/runs/${currentTerminalRunId}/logs?fetch_remote=true`);
    const logs = await res.text();
    document.getElementById('terminal-body').innerText = logs;
    showToast('Fetched latest logs from Kaggle', 'success');
  } catch (err) {
    showToast('Failed to fetch remote logs: ' + err.message, 'error');
  }
}

function downloadCurrentLogFile() {
  if (!currentTerminalRunId) {
    showToast('Select a run first to download logs', 'warning');
    return;
  }
  window.open(`/api/runs/${currentTerminalRunId}/logs/download`, '_blank');
}

function clearTerminal() {
  const terminalBody = document.getElementById('terminal-body');
  if (terminalBody) terminalBody.innerText = '';
}
