// Live Logs & WebSocket Terminal Module

let terminalSocket = null;
let currentTerminalRunId = null;

// Stream status line: pixel icon + text. Replaces the old "● LIVE STREAM
// CONNECTED" bullet so every state uses the same icon set as the rest of the
// dashboard. `tone` picks the Tailwind text colour, `iconCls` extra icon classes
// (e.g. animate-spin on the in-progress states).
function setStreamStatus(text, icon, tone = 'text-slate-400', iconCls = '') {
  const el = document.getElementById('terminal-stream-status');
  if (!el) return;
  el.className = `ml-2 font-mono text-[11px] ${tone}`;
  el.innerHTML = `<i data-lucide="${icon}" class="w-3 h-3 inline align-[-1px] ${iconCls}"></i><span class="align-middle">${esc(text)}</span>`;
  refreshIcons();
}

function updateTerminalRunDropdown() {
  const select = document.getElementById('terminal-run-select');
  if (!select) return;

  const runs = AppState.allRuns || [];
  let html = '<option value="">-- Select a run to view output logs --</option>';
  
  runs.forEach(r => {
    // No emoji: a native <option> cannot hold markup, so the status word in the
    // label is the only signal there.
    const selected = r.id === currentTerminalRunId ? 'selected' : '';
    const label = `[${r.account_username}] ${r.title} (${r.status})`;
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
    setStreamStatus("Stream Disconnected", "x", "text-slate-400");
    document.getElementById('terminal-run-info').innerText = "No Active Session";
  }
}

async function viewLogsForRun(runId) {
  // Always surface the terminal - callers may be on any tab (Run Catalog,
  // launch success, dashboard). Without this the stream connected invisibly.
  if (AppState.activeTab !== 'terminal') {
    switchTab('terminal');
  }

  currentTerminalRunId = runId;
  AppState.selectedTerminalRunId = runId;

  const run = AppState.allRuns.find(r => r.id === runId);
  if (run) {
    document.getElementById('terminal-run-info').innerHTML = `Kernel: <strong class="text-cyan-400 font-mono">${esc(run.kernel_ref)}</strong> | Accelerator: <span class="text-purple-300">${esc(run.accelerator)}</span>`;
  }

  updateTerminalRunDropdown();
  disconnectTerminalSocket();

  setStreamStatus("Loading logs...", "loader-2", "text-amber-400", "animate-spin");

  // 1. Load the stored log file over plain HTTP first - this ALWAYS works,
  //    even behind proxies that block or mangle WebSockets.
  await loadStoredLogs(runId);
  if (currentTerminalRunId !== runId) return; // user switched runs meanwhile

  // 2. Only attach a live stream while the run can still produce output.
  //    'cancelling' is finished for the run (nothing more will ever be logged),
  //    so it correctly falls through to the stored-log state.
  const isActive = run && (run.status === 'queued' || run.status === 'running');
  if (isActive) {
    connectTerminalSocket(runId, true); // stored logs already on screen
  } else {
    setStreamStatus("Stored Log Output", "archive", "text-slate-400");
  }
}

async function loadStoredLogs(runId) {
  const terminalBody = document.getElementById('terminal-body');
  if (!terminalBody) return;

  try {
    const res = await fetch(`/api/runs/${runId}/logs`);
    if (res.status === 401) { window.location.href = '/login'; return; }
    const text = await res.text();
    // Ignore stale responses if the user switched runs meanwhile
    if (currentTerminalRunId !== runId) return;
    terminalBody.textContent = text || '(log file is empty)';
    terminalBody.scrollTop = terminalBody.scrollHeight;
  } catch (err) {
    terminalBody.textContent = `Failed to load stored logs: ${err.message}\nUse "Fetch Full Kaggle Log" to pull them straight from Kaggle.`;
  }
}

function connectTerminalSocket(runId, skipInitial = false) {
  disconnectTerminalSocket();

  const terminalBody = document.getElementById('terminal-body');
  // Append a separator - never wipe logs already loaded over HTTP
  if (terminalBody) terminalBody.textContent += `\n--- Attaching live stream ---\n`;
  setStreamStatus("Connecting...", "loader-2", "text-amber-400", "animate-spin");

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/runs/${runId}/logs${skipInitial ? '?skip_initial=1' : ''}`;

  terminalSocket = new WebSocket(wsUrl);

  terminalSocket.onopen = () => {
    setStreamStatus("LIVE STREAM CONNECTED", "radio", "text-emerald-400");
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
    // Only report offline if we're still the active stream
    if (currentTerminalRunId === runId) {
      setStreamStatus("Stream Offline / Finished", "x", "text-slate-500");
    }
  };

  terminalSocket.onerror = () => {
    // Common cause: reverse proxy without WebSocket forwarding.
    // Stored logs are already on screen; remote fetch is the fallback.
    if (currentTerminalRunId === runId) {
      setStreamStatus("Live stream unavailable - use Fetch Full Kaggle Log", "alert-triangle", "text-rose-400");
    }
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
