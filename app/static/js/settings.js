// Settings & Telegram Bot Module

async function loadSettingsData() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (data.success) {
      const s = data.settings;
      if (s.telegram_bot_token_configured) {
        document.getElementById('settings-bot-token').placeholder = `Configured: ${s.telegram_bot_token_masked}`;
      }
      document.getElementById('settings-chat-id').value = s.telegram_chat_id || '';
    }
  } catch (err) {
    console.error('Error loading settings:', err);
  }
}

async function handleSaveTelegramSettings(e) {
  e.preventDefault();
  const token = document.getElementById('settings-bot-token').value;
  const chatId = document.getElementById('settings-chat-id').value;

  const payload = {
    telegram_chat_id: chatId
  };
  if (token.trim()) {
    payload.telegram_bot_token = token.trim();
  }

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast('Telegram settings saved successfully!', 'success');
      loadSettingsData();
    } else {
      showToast('Failed to save settings', 'error');
    }
  } catch (err) {
    showToast('Error saving settings: ' + err.message, 'error');
  }
}

async function sendTestTelegramMessage() {
  const token = document.getElementById('settings-bot-token').value;
  const chatId = document.getElementById('settings-chat-id').value;

  showToast('Dispatching test notification to Telegram...', 'info');

  try {
    const res = await fetch('/api/settings/telegram/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bot_token: token.trim() || null,
        chat_id: chatId.trim() || null
      })
    });
    const data = await res.json();

    if (res.ok && data.success) {
      showToast('Telegram test alert sent successfully! Check your channel.', 'success');
    } else {
      showToast(data.detail || data.error || 'Failed to send Telegram test', 'error');
    }
  } catch (err) {
    showToast('Telegram test failed: ' + err.message, 'error');
  }
}

// History Catalog rendering helper
function loadHistoryData() {
  renderHistory();
}

function renderHistory() {
  const tbody = document.getElementById('history-table-body');
  if (!tbody) return;

  const runs = AppState.allRuns || [];
  if (runs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-6 py-8 text-center text-slate-500">No notebook executions recorded yet. Launch your first run from the Single Run or Distributed tab.</td></tr>`;
    return;
  }

  tbody.innerHTML = runs.map(r => {
    const startStr = new Date(r.start_time).toLocaleString();
    const trialBadge = r.is_trial ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-900/60 text-amber-300 border border-amber-700/50 mr-1.5">TRIAL</span>' : '';
    const shardBadge = r.workload_id ? `<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-purple-900/60 text-purple-300 border border-purple-700/50 mr-1.5">SHARD ${r.shard_index + 1}/${r.total_shards}</span>` : '';

    const statusBadges = {
      running: '<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800">RUNNING</span>',
      queued: '<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-cyan-950 text-cyan-400 border border-cyan-800">QUEUED</span>',
      complete: '<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-blue-950 text-blue-400 border border-blue-800">COMPLETE</span>',
      error: '<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-rose-950 text-rose-400 border border-rose-800">ERROR</span>',
      stopped: '<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">STOPPED</span>'
    };

    const badge = statusBadges[r.status] || `<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-slate-800 text-slate-300">${r.status.toUpperCase()}</span>`;

    const stopButton = (r.status === 'running' || r.status === 'queued') 
      ? `<button onclick="stopRun('${r.id}')" class="px-2.5 py-1 rounded text-xs font-semibold bg-rose-600/20 text-rose-400 hover:bg-rose-600/40 border border-rose-500/30 transition">Stop</button>`
      : '';

    return `
      <tr class="hover:bg-slate-800/30 transition">
        <td class="px-6 py-4">
          <div class="font-bold text-white flex items-center">${trialBadge}${shardBadge} ${r.title}</div>
          <a href="${r.kaggle_url}" target="_blank" class="text-xs text-cyan-400 hover:underline flex items-center space-x-1 mt-0.5 font-mono">
            <span>${r.kernel_ref}</span>
            <i data-lucide="external-link" class="w-3 h-3 inline"></i>
          </a>
        </td>
        <td class="px-6 py-4 font-semibold text-slate-300">@${r.account_username}</td>
        <td class="px-6 py-4 font-mono text-xs text-purple-300">${r.accelerator}</td>
        <td class="px-6 py-4 text-xs text-slate-400 font-mono">${startStr}</td>
        <td class="px-6 py-4">${badge}</td>
        <td class="px-6 py-4 text-right space-x-2">
          <button onclick="viewLogsForRun('${r.id}')" class="px-2.5 py-1 rounded text-xs font-semibold bg-slate-800 text-slate-300 hover:text-white transition">
            Logs
          </button>
          <button onclick="inspectFilesForRun('${r.id}'); switchTab('files');" class="px-2.5 py-1 rounded text-xs font-semibold bg-amber-600/20 text-amber-400 hover:bg-amber-600/30 transition">
            Outputs
          </button>
          ${stopButton}
        </td>
      </tr>
    `;
  }).join('');
  lucide.createIcons();
}
