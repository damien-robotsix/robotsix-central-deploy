let logAbortController = null;
let gatewayBaseDomain = '';
const REFRESH_INTERVAL_MS = 30000;
let refreshTimer = null;

function updateRefreshTime() {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  document.getElementById('refresh-time').textContent = `Last refreshed: ${hh}:${mm}:${ss}`;
}

function showError(msg) {
  const el = document.getElementById('error-banner');
  el.textContent = msg;
  el.style.display = 'block';
}

function hideError() {
  const el = document.getElementById('error-banner');
  el.style.display = 'none';
}

function showWarning(msg) {
  const el = document.getElementById('warning-banner');
  document.getElementById('warning-banner-text').textContent = msg;
  el.style.display = 'block';
}

function hideWarning() {
  const el = document.getElementById('warning-banner');
  el.style.display = 'none';
}

function renderRow(svc) {
  const state = svc.state || 'unknown';
  const badgeClass = `badge-${state}`;
  const rev = svc.image_revision || '';
  const revShort = rev ? rev.slice(0, 12) : '\u2014';
  // Use rollup health for display when available; fall back to primary-only health.
  // Health values: see HealthStatus enum (lifecycle/models.py)
  const health = svc.overall_health || svc.health || '';
  let healthClass = 'health-unknown';
  if (health === 'healthy') healthClass = 'health-healthy';
  else if (health === 'unhealthy') healthClass = 'health-unhealthy';
  const healthDisplay = health || '\u2014';

  // Build per-container breakdown tooltip when siblings are present.
  let healthTitle = '';
  if (svc.sibling_health && svc.sibling_health.length > 0) {
    const lines = [
      `primary: ${svc.health || '\u2014'}`,
      ...(svc.sibling_health).map(s => `${s.name}: ${s.health || '\u2014'}`),
    ];
    healthTitle = lines.join('\n');
  }
  const lastError = svc.last_error || '';

  let errorHtml = '';
  if (lastError) {
    errorHtml = `<span class="error-detail" title="${escAttr(lastError)}">${escHtml(lastError.slice(0, 80))}${lastError.length > 80 ? '…' : ''}</span>`;
  }

  // Up-to-date badge
  // Update-state values: see UpdateState enum (lifecycle/models.py)
  const updateState = svc.update_state || 'unknown';
  let updateBadge = '';
  if (updateState === 'up-to-date') {
    updateBadge = '<span class="badge badge-update-ok">up to date</span>';
  } else if (updateState === 'update-available') {
    const runningShort = (svc.running_digest || '').replace(/^sha256:/, '').slice(0, 12);
    const latestShort = (svc.latest_digest || '').replace(/^sha256:/, '').slice(0, 12);
    const tooltip = `running ${runningShort || '?'} ≠ latest ${latestShort || '?'}`;
    updateBadge = `<span class="badge badge-update-avail" title="${escAttr(tooltip)}">update available</span>`;
  } else {
    updateBadge = '<span class="badge badge-update-unknown">unknown</span>';
  }

  // Untracked badge (no repo_id → no mill issue tracking)
  const untrackedBadge = (!svc.repo_id) ? ' <span class="badge badge-untracked">untracked</span>' : '';

  // Gateway shortcut
  const gatewayHref = gatewayBaseDomain
    ? `https://${escAttr(svc.name)}.${gatewayBaseDomain}/`
    : `/${escAttr(svc.name)}/`;
  const openLink = svc.state === 'running'
    ? `<a href="${gatewayHref}" target="_blank" rel="noopener"
           style="font-size:0.8rem;color:var(--blue);">↗ Open</a>`
    : '<span style="color:#666;">—</span>';

  return `<tr id="row-${escAttr(svc.name)}">
    <td>${escHtml(svc.name)}${untrackedBadge}</td>
    <td><span class="badge ${badgeClass}">${escHtml(state)}</span>${errorHtml}</td>
    <td><span class="revision" title="${escAttr(svc.running_digest || rev)}">${escHtml(revShort)}</span></td>
    <td><span class="${healthClass}" title="${escAttr(healthTitle)}">${escHtml(healthDisplay)}</span></td>
    <td>${updateBadge}</td>
    <td class="actions">
      <button onclick="doAction('${escAttr(svc.name)}', 'start')" id="btn-start-${escAttr(svc.name)}">Start</button>
      <button onclick="doAction('${escAttr(svc.name)}', 'stop')" id="btn-stop-${escAttr(svc.name)}">Stop</button>
      <button onclick="doAction('${escAttr(svc.name)}', 'restart')" id="btn-restart-${escAttr(svc.name)}">Restart</button>
      <button onclick="updateService('${escAttr(svc.name)}')" id="btn-update-${escAttr(svc.name)}" class="btn-primary" style="font-size:0.78rem;" title="Force-pull the latest image and recreate the container">Update</button>
      <button onclick="openHistoryModal('${escAttr(svc.name)}')" id="btn-history-${escAttr(svc.name)}" style="font-size:0.78rem;" title="View deploy history and rollback">History</button>
      ${svc.has_config_yaml
        ? `<button onclick="openConfigModal('${escAttr(svc.name)}')" class="btn-primary" style="font-size:0.78rem;">Configure</button>
           <button onclick="openEnvModal('${escAttr(svc.name)}')" class="btn-secondary">Env &amp; Secrets</button>`
        : `<button onclick="openEnvModal('${escAttr(svc.name)}')" class="btn-secondary">Config</button>`
      }
      <button class="btn-danger" onclick="doRemove('${escAttr(svc.name)}')">Remove</button>
      <span class="inline-error" id="err-${escAttr(svc.name)}" style="display:none;"></span>
    </td>
    <td>${openLink}</td>
    <td><a href="#" class="logs-link" onclick="openLogs('${escAttr(svc.name)}'); return false;">Logs</a></td>
  </tr>`;
}

function renderSiblingRow(svc) {
  const state = svc.state || 'unknown';
  const badgeClass = `badge-${state}`;
  const rev = svc.image_revision || '';
  const revShort = rev ? rev.slice(0, 12) : '\u2014';
  const health = svc.health || '';
  let healthClass = 'health-unknown';
  if (health === 'healthy') healthClass = 'health-healthy';
  else if (health === 'unhealthy') healthClass = 'health-unhealthy';
  const healthDisplay = health || '\u2014';

  const updateState = svc.update_state || 'unknown';
  let updateBadge = '';
  if (updateState === 'up-to-date') {
    updateBadge = '<span class="badge badge-update-ok">up to date</span>';
  } else if (updateState === 'update-available') {
    const runningShort = (svc.running_digest || '').replace(/^sha256:/, '').slice(0, 12);
    const latestShort = (svc.latest_digest || '').replace(/^sha256:/, '').slice(0, 12);
    const tooltip = `running ${runningShort || '?'} ≠ latest ${latestShort || '?'}`;
    updateBadge = `<span class="badge badge-update-avail" title="${escAttr(tooltip)}">update available</span>`;
  } else {
    updateBadge = '<span class="badge badge-update-unknown">unknown</span>';
  }

  const gatewayHref = gatewayBaseDomain
    ? `https://${escAttr(svc.name)}.${gatewayBaseDomain}/`
    : `/${escAttr(svc.name)}/`;
  const openLink = svc.state === 'running'
    ? `<a href="${gatewayHref}" target="_blank" rel="noopener"
           style="font-size:0.8rem;color:var(--blue);">↗ Open</a>`
    : '<span style="color:#666;">—</span>';

  return `<tr class="sibling-row" id="row-${escAttr(svc.name)}">
    <td>↳ ${escHtml(svc.name)}</td>
    <td><span class="badge ${badgeClass}">${escHtml(state)}</span></td>
    <td><span class="revision" title="${escAttr(svc.running_digest || rev)}">${escHtml(revShort)}</span></td>
    <td><span class="${healthClass}">${escHtml(healthDisplay)}</span></td>
    <td>${updateBadge}</td>
    <td class="actions"></td>
    <td>${openLink}</td>
    <td><a href="#" class="logs-link" onclick="openLogs('${escAttr(svc.name)}'); return false;">Logs</a></td>
  </tr>`;
}

function setButtonsDisabled(name, disabled) {
  ['start', 'stop', 'restart'].forEach(action => {
    const btn = document.getElementById(`btn-${action}-${name}`);
    if (btn) btn.disabled = disabled;
  });
}

function showRowError(name, msg) {
  const el = document.getElementById(`err-${name}`);
  if (el) {
    el.textContent = msg;
    el.style.display = 'block';
  }
}

function hideRowError(name) {
  const el = document.getElementById(`err-${name}`);
  if (el) {
    el.style.display = 'none';
  }
}

async function fetchOneStatus(name) {
  const resp = await fetch(`/services/${encodeURIComponent(name)}`, { credentials: 'same-origin' });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function authHeaders() {
  // Browser sends Basic Auth automatically after initial authentication.
  return {};
}

async function fetchDiskUsage() {
    try {
        const resp = await fetch('/disk', { headers: authHeaders(), credentials: 'same-origin' });
        if (!resp.ok) { document.getElementById('disk-content').textContent = 'Error loading disk info.'; return; }
        renderDiskPanel(await resp.json());
    } catch (e) {
        document.getElementById('disk-content').textContent = 'Error: ' + e.message;
    }
}

async function reclaimBuildCache() {
    const btn = document.getElementById('reclaim-btn');
    if (!btn) return;
    btn.disabled = true;
    const toast = document.getElementById('disk-toast');
    try {
        const resp = await fetch('/disk/reclaim', {
            method: 'POST',
            headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
            credentials: 'same-origin',
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        const freed = fmt_bytes(data.space_reclaimed_bytes || 0);
        toast.textContent = `\u2713 Reclaimed ${freed}.`;
        toast.style.color = 'var(--green)';
        await fetchDiskUsage();
    } catch (e) {
        toast.textContent = `Reclaim failed: ${e.message}`;
        toast.style.color = 'var(--red)';
    } finally {
        btn.disabled = false;
    }
}

function fmt_gb(bytes) { return (bytes / 1073741824).toFixed(1) + ' GiB'; }

function fmt_bytes(bytes) {
    if (bytes === null || bytes === undefined) return '\u2014';
    const abs = Math.abs(bytes);
    if (abs < 1024)        return bytes.toFixed(0) + '\u00a0B';
    if (abs < 1_048_576)   return (bytes / 1024).toFixed(1) + '\u00a0KB';
    if (abs < 1_073_741_824) return (bytes / 1_048_576).toFixed(1) + '\u00a0MB';
    if (abs < 1_099_511_627_776) return (bytes / 1_073_741_824).toFixed(1) + '\u00a0GB';
    return (bytes / 1_099_511_627_776).toFixed(1) + '\u00a0TB';
}

function renderDiskPanel(data) {
    const usedPct = Math.round(data.used_bytes / data.total_bytes * 100);
    const freePct = data.total_bytes > 0 ? data.free_bytes / data.total_bytes * 100 : 100;
    const warn = freePct < data.warn_threshold_pct;
    document.getElementById('disk-warning').textContent =
      `⚠ Low disk space — free space is below ${data.warn_threshold_pct}%!`;
    document.getElementById('disk-warning').style.display = warn ? '' : 'none';
    const barClass = warn ? 'disk-bar-fill warn' : 'disk-bar-fill';
    const vols = (data.docker.volumes || []).slice().sort((a, b) => b.size_bytes - a.size_bytes);
    const volRows = vols.map(v =>
        `<tr><th style="font-weight:400;"><span class="volume-name-cell" onclick="openVolumeBrowser('${escAttr(v.name)}')">${escHtml(v.name)}</span>${v.in_use ? '' : ' <span style="color:#64748b;">(unused)</span>'}</th><td>${fmt_bytes(v.size_bytes)}</td></tr>`
    ).join('');
    const volTable = vols.length
        ? `<table style="margin-top:10px;"><tr><th colspan="2" style="text-align:left;color:#94a3b8;">Docker volumes</th></tr>${volRows}</table>`
        : '';
    document.getElementById('disk-content').innerHTML = `
        <table>
          <tr><th>Host used</th><td>${fmt_bytes(data.used_bytes)}
            <span class="disk-bar-wrap"><span class="${barClass}" style="width:${usedPct}%"></span></span>
            ${usedPct}%</td></tr>
          <tr><th>Docker images (host total)</th><td>${fmt_bytes(data.docker.images_size_bytes)}</td></tr>
          <tr><th>Docker build cache</th><td>${fmt_bytes(data.docker.build_cache_size_bytes)}</td></tr>
          <tr><th>Build cache reclaimable</th><td>${fmt_bytes(data.docker.build_cache_reclaimable_bytes)}${data.docker.build_cache_reclaimable_bytes > 0 ? ` <button id="reclaim-btn" class="reclaim-btn" onclick="reclaimBuildCache()">Reclaim</button>` : ''}</td></tr>
        </table>${volTable}`;
}

function fmt_mb(bytes) { return (bytes / 1048576).toFixed(1) + ' MiB'; }

async function fetchVolumeAudit() {
  try {
    const resp = await fetch('/volumes/audit', {
      headers: authHeaders(),
      credentials: 'same-origin',
    });
    if (!resp.ok) return;
    const data = await resp.json();
    const panel = document.getElementById('volume-audit-panel');
    if (!data.enabled) {
      panel.style.display = 'none';
      return;
    }
    panel.style.display = '';
    renderVolumeAuditPanel(data);
  } catch (_e) {
    // silently skip — audit panel is optional
  }
}

function renderVolumeAuditPanel(data) {
  // Timestamp
  const ts = document.getElementById('volume-audit-timestamp');
  ts.textContent = data.last_scan_at
    ? 'Last scan: ' + new Date(data.last_scan_at).toLocaleString()
    : 'No scan yet';

  // Volume table
  const content = document.getElementById('volume-audit-content');
  if (!data.volumes || data.volumes.length === 0) {
    content.textContent = 'No volume data yet.';
  } else {
    const rows = data.volumes.map(v => {
      const size = fmt_mb(v.size_bytes);
      const delta = v.delta_bytes !== null
        ? (v.delta_bytes >= 0 ? '+' : '') + fmt_mb(v.delta_bytes)
        : '\u2014';
      const pct = v.growth_pct !== null ? v.growth_pct.toFixed(1) + '%' : '\u2014';
      const status = v.flagged
        ? '<span class="badge badge-warn">\u26A0 FLAGGED</span>'
        : '<span class="badge badge-ok">OK</span>';
      return `<tr>
        <td>${escHtml(v.volume_name)}</td>
        <td>${escHtml(v.component_id || '\u2014')}</td>
        <td>${size}</td>
        <td>${delta}</td>
        <td>${pct}</td>
        <td>${status}</td>
      </tr>`;
    }).join('');
    content.innerHTML = `
      <table class="disk-table">
        <thead><tr>
          <th>Volume</th><th>Component</th><th>Size</th>
          <th>Delta</th><th>Growth %</th><th>Status</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // Recent findings section
  const findingsDiv = document.getElementById('volume-audit-findings');
  const findingsList = document.getElementById('volume-audit-findings-list');
  const findings = (data.recent_findings || []).slice(-5);
  if (findings.length > 0) {
    findingsDiv.style.display = '';
    findingsList.innerHTML = findings.map(f =>
      `<li>${escHtml(f.finding_at ? new Date(f.finding_at).toLocaleString() : '')} \u2014
           ${escHtml(f.detail || f.volume_name)}</li>`
    ).join('');
  } else {
    findingsDiv.style.display = 'none';
  }
}

async function fetchOrphanVolumes() {
  try {
    const resp = await fetch('/volumes/orphans', {
      headers: authHeaders(),
      credentials: 'same-origin',
    });
    if (!resp.ok) return;
    renderOrphanVolumes(await resp.json());
  } catch (_e) {
    // silently skip — orphan panel is optional
  }
}

function renderOrphanVolumes(data) {
  const panel = document.getElementById('orphan-volumes-panel');
  const vols = data.volumes || [];
  if (vols.length === 0) {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = '';
  const rows = vols.slice().sort((a, b) => b.size_bytes - a.size_bytes).map(v =>
    `<tr><th style="font-weight:400;text-align:left;">${escHtml(v.name)}</th><td style="text-align:right;">${fmt_bytes(v.size_bytes)}</td></tr>`
  ).join('');
  document.getElementById('orphan-volumes-content').innerHTML = `
    <table style="border-collapse:collapse;min-width:400px;">
      <tr><th colspan="2" style="text-align:left;color:#94a3b8;padding:4px 12px;">
        ${vols.length} orphan volume${vols.length === 1 ? '' : 's'} &middot; ${fmt_bytes(data.total_bytes)}
        <button id="prune-vols-btn" class="reclaim-btn" onclick="pruneOrphanVolumes()">Prune all</button>
      </th></tr>
      ${rows}
    </table>`;
}

async function pruneOrphanVolumes() {
  const btn = document.getElementById('prune-vols-btn');
  const toast = document.getElementById('orphan-volumes-toast');
  if (!confirm('Permanently delete all orphan volumes? This destroys their data and cannot be undone.')) return;
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch('/volumes/prune', {
      method: 'POST',
      headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify({}),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const freed = fmt_bytes(data.space_reclaimed_bytes || 0);
    let msg = `✓ Removed ${data.removed.length} volume${data.removed.length === 1 ? '' : 's'} (${freed}).`;
    if (data.failed && data.failed.length) msg += ` ${data.failed.length} could not be removed.`;
    toast.textContent = msg;
    toast.style.color = 'var(--green)';
    await fetchOrphanVolumes();
    await fetchDiskUsage();
  } catch (e) {
    toast.textContent = `Prune failed: ${e.message}`;
    toast.style.color = 'var(--red)';
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadDashboard() {
  hideError();
  fetchDiskUsage(); // fire-and-forget disk refresh alongside services
  fetchVolumeAudit(); // fire-and-forget volume audit refresh
  fetchOrphanVolumes(); // fire-and-forget orphan-volume refresh
  try {
    const resp = await fetch('/services', { credentials: 'same-origin' });
    if (!resp.ok) {
      if (resp.status === 401) {
        // Re-trigger auth challenge by navigating to /ui
        window.location.href = '/ui';
        return;
      }
      const body = await resp.json().catch(() => ({}));
      showError(body.error || `Failed to load services (HTTP ${resp.status})`);
      return;
    }
    const data = await resp.json();
    const services = data.services || [];

    // Partition: primaries have component_id === '', siblings have component_id set.
    const primaryList = services.filter(s => s.component_id === '');
    const siblingMap = new Map();  // primary_name → svc[]
    for (const s of services.filter(s => s.component_id !== '')) {
      const list = siblingMap.get(s.component_id) || [];
      list.push(s);
      siblingMap.set(s.component_id, list);
    }

    // Build a status-by-name map from the parallel fetch.
    const allNames = services.map(s => s.name);
    const allResults = await Promise.allSettled(allNames.map(n => fetchOneStatus(n)));
    const statusByName = new Map();
    for (let i = 0; i < allNames.length; i++) {
      statusByName.set(allNames[i], allResults[i]);
    }

    const rows = [];
    for (const svc of primaryList) {
      const r = statusByName.get(svc.name);
      rows.push(r?.status === 'fulfilled'
        ? renderRow(r.value)
        : `<tr id="row-${escAttr(svc.name)}"><td>${escHtml(svc.name)}</td><td><span class="badge badge-unknown">unknown</span></td><td>—</td><td>—</td><td>—</td><td></td><td></td><td></td></tr>`);
      for (const sibSvc of siblingMap.get(svc.name) || []) {
        const sr = statusByName.get(sibSvc.name);
        rows.push(sr?.status === 'fulfilled'
          ? renderSiblingRow(sr.value)
          : `<tr class="sibling-row" id="row-${escAttr(sibSvc.name)}"><td>↳ ${escHtml(sibSvc.name)}</td><td><span class="badge badge-unknown">unknown</span></td><td>—</td><td>—</td><td>—</td><td></td><td></td><td></td></tr>`);
      }
    }
    // Orphaned siblings (primary absent from list) fall through as top-level rows.
    for (const svc of services.filter(s => s.component_id !== '' && !primaryList.some(p => p.name === s.component_id))) {
      const r = statusByName.get(svc.name);
      rows.push(r?.status === 'fulfilled'
        ? renderRow(r.value)
        : `<tr id="row-${escAttr(svc.name)}"><td>${escHtml(svc.name)}</td><td><span class="badge badge-unknown">unknown</span></td><td>—</td><td>—</td><td>—</td><td></td><td></td><td></td></tr>`);
    }

    document.getElementById('table-body').innerHTML = rows.join('');
    updateRefreshTime();
  } catch (err) {
    showError(`Dashboard error: ${err.message}`);
  }
}

var DEPLOY_PHASE_LABELS = {
  deploying: 'Deploying\u2026',
  waiting_health: 'Waiting for health\u2026',
  deploying_siblings: 'Deploying siblings\u2026',
  done: 'Done',
  failed: 'Failed',
};

var _deployJobPollTimers = {};

function stopDeployJobPoll(name) {
  if (_deployJobPollTimers[name] !== undefined) {
    clearTimeout(_deployJobPollTimers[name]);
    delete _deployJobPollTimers[name];
  }
}

function pollDeployJob(name, jobId, btn, origText) {
  stopDeployJobPoll(name);

  function poll() {
    _deployJobPollTimers[name] = setTimeout(async function() {
      try {
        var res = await fetch('/services/deploy-jobs/' + encodeURIComponent(jobId), {
          headers: authHeaders(),
          credentials: 'same-origin',
        });
        if (res.status === 404) {
          stopDeployJobPoll(name);
          showRowError(name, 'Deploy job lost \u2014 server may have restarted.');
          if (btn) { btn.disabled = false; btn.textContent = origText; }
          return;
        }
        if (!res.ok) {
          stopDeployJobPoll(name);
          var errData = await res.json().catch(function() { return {}; });
          showRowError(name, 'Deploy failed: ' + (errData.error || 'HTTP ' + res.status));
          if (btn) { btn.disabled = false; btn.textContent = origText; }
          return;
        }
        var data = await res.json();
        var phase = data.phase;
        if (btn) { btn.textContent = DEPLOY_PHASE_LABELS[phase] || phase; }

        if (phase === 'done') {
          stopDeployJobPoll(name);
          if (btn) { btn.disabled = false; btn.textContent = origText; }
          // Refresh the row
          try {
            var svc = await fetchOneStatus(name);
            var rowEl = document.getElementById('row-' + name);
            if (rowEl) rowEl.outerHTML = renderRow(svc);
          } catch (e) { /* best-effort */ }
          updateRefreshTime();
          if (data.warnings && data.warnings.length > 0) {
            showWarning('\u26a0\ufe0f ' + data.warnings.join(' '));
          }
        } else if (phase === 'failed') {
          stopDeployJobPoll(name);
          showRowError(name, 'Deploy failed: ' + (data.error || 'unknown error'));
          if (btn) { btn.disabled = false; btn.textContent = origText; }
          // Try to refresh the row anyway
          try {
            var svc2 = await fetchOneStatus(name);
            var rowEl2 = document.getElementById('row-' + name);
            if (rowEl2) rowEl2.outerHTML = renderRow(svc2);
          } catch (e) { /* best-effort */ }
          updateRefreshTime();
        } else {
          // Still in progress — keep polling.
          poll();
        }
      } catch (err) {
        stopDeployJobPoll(name);
        showRowError(name, 'Deploy poll error: ' + err.message);
        if (btn) { btn.disabled = false; btn.textContent = origText; }
      }
    }, 1500);
  }
  poll();
}

async function updateService(name) {
  if (!window.confirm(`Force-update "${name}" to the latest image?\n\nThis pulls the latest image from the registry and recreates the container (brief restart).`)) return;
  hideRowError(name);
  hideWarning();
  const btn = document.getElementById(`btn-update-${name}`);
  const orig = btn ? btn.textContent : 'Update';
  if (btn) { btn.disabled = true; btn.textContent = 'Deploying\u2026'; }
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/deploy`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: '{}',
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const body = await resp.json().catch(() => ({}));
    // 202 Accepted — poll the deploy job for progress.
    pollDeployJob(name, body.job_id, btn, orig);
  } catch (err) {
    showRowError(name, 'Update failed: ' + err.message);
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

async function doAction(name, action) {
  hideRowError(name);
  hideWarning();
  setButtonsDisabled(name, true);
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/${action}`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const body = await resp.json().catch(() => ({}));
    // Refresh just this row
    const svc = await fetchOneStatus(name);
    const rowEl = document.getElementById(`row-${name}`);
    if (rowEl) {
      rowEl.outerHTML = renderRow(svc);
    }
    updateRefreshTime();
    if (body.warnings && body.warnings.length > 0) {
      showWarning('\u26a0\ufe0f ' + body.warnings.join(' '));
    }
  } catch (err) {
    showRowError(name, err.message);
    // Try to refresh the row anyway to get current state
    try {
      const svc = await fetchOneStatus(name);
      const rowEl = document.getElementById(`row-${name}`);
      if (rowEl) {
        rowEl.outerHTML = renderRow(svc);
      }
    } catch (_) { /* ignore */ }
  } finally {
    setButtonsDisabled(name, false);
    startAutoRefresh();
  }
}

let _removeTarget = null;

// Open the Remove modal. Data volumes are PRESERVED by default; deleting them
// is an explicit, separately-warned opt-in (some components must keep their
// data across a re-add).
function doRemove(name) {
  _removeTarget = name;
  document.getElementById('remove-modal-component').textContent = name;
  document.getElementById('remove-stop-container').checked = true;
  document.getElementById('remove-delete-volumes').checked = false;
  document.getElementById('remove-modal-error').textContent = '';
  updateRemoveBtn();
  document.getElementById('remove-modal').classList.add('open');
}

function updateRemoveBtn() {
  const del = document.getElementById('remove-delete-volumes').checked;
  const btn = document.getElementById('remove-confirm-btn');
  btn.textContent = del ? '⚠ Remove + DELETE data volumes' : 'Remove component';
}

function closeRemoveModal() {
  document.getElementById('remove-modal').classList.remove('open');
  _removeTarget = null;
}

async function confirmRemove() {
  const name = _removeTarget;
  if (!name) return;
  const stopContainer = document.getElementById('remove-stop-container').checked;
  const removeVolumes = document.getElementById('remove-delete-volumes').checked;
  let url = `/services/${encodeURIComponent(name)}?stop_container=${stopContainer}`;
  if (removeVolumes) url += `&remove_volumes=true`;
  const btn = document.getElementById('remove-confirm-btn');
  btn.disabled = true;
  try {
    const resp = await fetch(url, { method: 'DELETE', credentials: 'same-origin' });
    if (resp.status === 204) {
      closeRemoveModal();
      await loadDashboard();
      return;
    }
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    document.getElementById('remove-modal-error').textContent =
      `Remove failed (${resp.status}): ${err.detail || resp.statusText}`;
  } catch (e) {
    document.getElementById('remove-modal-error').textContent = `Remove error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function openLogs(name) {
  document.getElementById('log-modal-component').textContent = name;
  document.getElementById('log-output').textContent = '';
  document.getElementById('log-status').textContent = 'Connecting\u2026';
  document.getElementById('log-modal').classList.add('open');
  streamLogs(name);
}

function closeLogs() {
  document.getElementById('log-modal').classList.remove('open');
  if (logAbortController) {
    logAbortController.abort();
    logAbortController = null;
  }
}

async function streamLogs(name) {
  if (logAbortController) logAbortController.abort();
  logAbortController = new AbortController();
  const signal = logAbortController.signal;
  const statusEl = document.getElementById('log-status');
  const outputEl = document.getElementById('log-output');
  const bodyEl = outputEl.parentElement;

  try {
    const resp = await fetch(
      `/services/${encodeURIComponent(name)}/logs?tail=200&follow=true`,
      { credentials: 'same-origin', signal }
    );
    if (!resp.ok) {
      statusEl.textContent = `Error: HTTP ${resp.status}`;
      return;
    }
    statusEl.textContent = 'Streaming\u2026';

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let atBottom = true;

    // Track whether user has scrolled away from the bottom
    bodyEl.addEventListener('scroll', () => {
      atBottom = bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight < 32;
    }, { passive: true });

    while (true) {
      const { done, value } = await reader.read();
      if (done) { statusEl.textContent = 'Stream ended.'; break; }
      outputEl.textContent += decoder.decode(value, { stream: true });
      if (atBottom) bodyEl.scrollTop = bodyEl.scrollHeight;
    }
  } catch (err) {
    if (err.name === 'AbortError') return; // normal close — suppress
    statusEl.textContent = `Error: ${err.message}`;
  }
}

// ── Env / secrets modal ────────────────────────────────────────────

let currentEnvComponent = null;

function openEnvModal(name) {
  currentEnvComponent = name;
  document.getElementById('env-modal-component').textContent = name;
  document.getElementById('env-modal-error').style.display = 'none';
  document.getElementById('env-rows').innerHTML = '';
  document.getElementById('secret-rows').innerHTML = '';
  document.getElementById('env-modal').classList.add('open');

  fetchEnvConfig(name);
}

function closeEnvModal() {
  document.getElementById('env-modal').classList.remove('open');
  currentEnvComponent = null;
}

async function fetchEnvConfig(name) {
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/env`, {
      credentials: 'same-origin',
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    renderEnvRows(data.env || {}, data.secrets || {});
    document.getElementById('mem-limit-input').value = data.mem_limit || '2g';
    document.getElementById('chat-access-checkbox').checked = !!data.allow_chat_access;
    document.getElementById('claude-mount-checkbox').checked = !!data.claude_mount;
  } catch (err) {
    showEnvModalError(err.message);
  }
}

function renderEnvRows(env, secrets) {
  const envContainer = document.getElementById('env-rows');
  const secretContainer = document.getElementById('secret-rows');
  envContainer.innerHTML = '';
  secretContainer.innerHTML = '';

  for (const [key, value] of Object.entries(env)) {
    envContainer.appendChild(buildEnvRow(key, value));
  }

  for (const key of Object.keys(secrets)) {
    secretContainer.appendChild(buildSecretRow(key));
  }
}

function buildEnvRow(key, value) {
  const div = document.createElement('div');
  div.className = 'env-row';
  div.dataset.key = key;
  div.innerHTML = `
    <input type="text" class="env-key" value="${escAttr(key)}" readonly>
    <input type="text" class="env-value" value="${escAttr(value)}">
    <span class="inline-error" style="display:none;"></span>
  `;
  return div;
}

function buildSecretRow(key) {
  const div = document.createElement('div');
  div.className = 'env-row';
  div.dataset.key = key;
  div.innerHTML = `
    <span class="env-key" style="display:flex;align-items:center;">${escHtml(key)}</span>
    <span class="secret-value-masked">***</span>
    <input type="password" class="secret-new-value" placeholder="enter new value to update" autocomplete="off">
    <span class="inline-error" style="display:none;"></span>
  `;
  return div;
}





function showRowInlineError(row, msg) {
  const errEl = row.querySelector('.inline-error');
  if (errEl) {
    errEl.textContent = msg;
    errEl.style.display = 'block';
  }
}

async function syncEnvKeys() {
  hideEnvModalError();
  const name = currentEnvComponent;
  if (!name) return;
  const btn = document.getElementById('env-sync-keys-btn');
  btn.disabled = true;
  btn.textContent = 'Syncing…';
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/env/sync-keys`, {
      method: 'POST', credentials: 'same-origin', headers: authHeaders(),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
    await fetchEnvConfig(name);  // re-render with any newly seeded keys
    const added = (body.added_env || []).length + (body.added_secrets || []).length;
    const stale = (body.undeclared || []);
    let msg = added ? `Added ${added} new key(s) from the repo contract.` : 'No new keys — store matches the contract.';
    if (stale.length) msg += ` Note: ${stale.length} stored key(s) no longer declared by the repo: ${stale.join(', ')}.`;
    showEnvModalError(msg);  // reuse the banner as an info line
  } catch (err) {
    showEnvModalError('Key sync failed: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Sync keys from repo';
  }
}

async function saveEnvChanges() {
  hideEnvModalError();
  const name = currentEnvComponent;
  if (!name) return;

  // Collect env rows
  const env = {};
  const envRows = document.querySelectorAll('#env-rows .env-row');
  for (const row of envRows) {
    const keyInput = row.querySelector('.env-key');
    const valueInput = row.querySelector('.env-value');
    const key = (keyInput.value || '').trim();
    if (!key) continue;
    env[key] = valueInput ? valueInput.value : '';
  }

  // Collect secret rows — only include when new-value input is non-empty
  const secrets = {};
  const secretRows = document.querySelectorAll('#secret-rows .env-row');
  for (const row of secretRows) {
    const newValueInput = row.querySelector('.secret-new-value');
    const key = row.dataset.key || '';
    if (!key) continue;
    const newValue = newValueInput ? newValueInput.value : '';
    if (!newValue) continue; // skip unchanged secrets
    secrets[key] = newValue;
  }

  try {
    const memLimitInput = document.getElementById('mem-limit-input');
    const body = { env, secrets };
    if (memLimitInput && memLimitInput.value.trim()) {
      body.mem_limit = memLimitInput.value.trim();
    }
    body.allow_chat_access = document.getElementById('chat-access-checkbox').checked;
    body.claude_mount = document.getElementById('claude-mount-checkbox').checked;
    const resp = await fetch(`/services/${encodeURIComponent(name)}/env`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.status === 204) {
      closeEnvModal();
    } else {
      const body = await resp.json().catch(() => ({}));
      showEnvModalError(body.error || `HTTP ${resp.status}`);
    }
  } catch (err) {
    showEnvModalError(err.message);
  }
}

function showEnvModalError(msg) {
  const el = document.getElementById('env-modal-error');
  el.textContent = msg;
  el.style.display = 'block';
}

function hideEnvModalError() {
  document.getElementById('env-modal-error').style.display = 'none';
}

// ── Config modal ───────────────────────────────────────────────────

let _configComponent = null;
let _configSchema = null;
let _configAssistCommand = null;   // string|null
let _configAssistSeeds = [];       // {key, label}[]
let _componentSuggestions = [];    // [{id, container_name, container_port}]
let _configRawMode = false;

function openConfigModal(name) {
  _configComponent = name;
  _configSchema = null;
  _configAssistCommand = null;
  _configAssistSeeds = [];
  _componentSuggestions = [];
  _configRawMode = false;
  document.getElementById('config-modal-component').textContent = name;
  document.getElementById('config-modal-error').style.display = 'none';
  document.getElementById('config-form-body').innerHTML = '';
  document.getElementById('config-raw-body').style.display = 'none';
  document.getElementById('config-raw-body').value = '';
  document.getElementById('config-form-body').style.display = '';
  document.getElementById('config-raw-toggle-btn').textContent = '{ } Raw';
  // Remove any stale drift banner / conflict panel
  const driftBanner = document.getElementById('config-drift-banner');
  if (driftBanner) driftBanner.remove();
  const conflictPanel = document.getElementById('config-drift-conflict');
  if (conflictPanel) conflictPanel.remove();
  // Re-enable form inputs and Save button in case they were disabled
  document.querySelector('#config-modal .btn-primary').disabled = false;
  document.getElementById('config-modal').style.display = 'flex';

  fetchConfigSchema(name);
}

function closeConfigModal() {
  document.getElementById('config-modal').style.display = 'none';
  _configComponent = null;
  _configSchema = null;
  _configAssistCommand = null;
  _configAssistSeeds = [];
  _componentSuggestions = [];
  _closeSuggestDropdown();
  // Remove any stale drift banner / conflict panel
  const driftBanner = document.getElementById('config-drift-banner');
  if (driftBanner) driftBanner.remove();
  const conflictPanel = document.getElementById('config-drift-conflict');
  if (conflictPanel) conflictPanel.remove();
  // Re-enable form inputs for next open
  document.querySelector('#config-modal .btn-primary').disabled = false;
  const formBody = document.getElementById('config-form-body');
  if (formBody) {
    formBody.querySelectorAll('input, select').forEach(el => { el.disabled = false; });
  }
}

function toggleConfigMode() {
  _configRawMode = !_configRawMode;
  const formBody = document.getElementById('config-form-body');
  const rawBody = document.getElementById('config-raw-body');
  const toggleBtn = document.getElementById('config-raw-toggle-btn');
  const saveBtn = document.querySelector('#config-modal .btn-primary');
  const driftBanner = document.getElementById('config-drift-banner');
  const conflictPanel = document.getElementById('config-drift-conflict');

  if (_configRawMode) {
    // Switch to raw JSON mode: collect current form values and show as JSON
    const currentValues = collectConfigValues(_configSchema);
    rawBody.value = JSON.stringify(currentValues, null, 2);
    formBody.style.display = 'none';
    rawBody.style.display = '';
    toggleBtn.textContent = '📋 Form';
    saveBtn.disabled = false;
    // Hide drift UI in raw mode
    if (driftBanner) driftBanner.style.display = 'none';
    if (conflictPanel) conflictPanel.style.display = 'none';
  } else {
    // Switch back to form mode: parse raw JSON and regenerate form
    let parsed = {};
    try {
      parsed = JSON.parse(rawBody.value.trim() || '{}');
    } catch (_) {
      showConfigModalError('Invalid JSON — fix syntax errors before switching to Form view.');
      _configRawMode = true;  // stay in raw mode
      return;
    }
    hideConfigModalError();
    generateConfigForm(_configSchema, parsed);
    _injectConfigAssistUI();
    rawBody.style.display = 'none';
    formBody.style.display = '';
    toggleBtn.textContent = '{ } Raw';
    saveBtn.disabled = false;
    // Restore drift UI if applicable
    if (driftBanner) driftBanner.style.display = '';
    if (conflictPanel) conflictPanel.style.display = '';
  }
}

async function refreshConfigSchema() {
  if (!_configComponent) return;
  const btn = document.getElementById('config-refresh-schema-btn');
  btn.disabled = true;
  btn.textContent = 'Refreshing…';
  try {
    const resp = await fetch(
      `/services/${encodeURIComponent(_configComponent)}/config/refresh-schema`,
      { method: 'POST', headers: authHeaders(), credentials: 'same-origin' }
    );
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
    await fetchConfigSchema(_configComponent);  // re-render with the fresh schema
  } catch (err) {
    showConfigModalError('Schema refresh failed: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Refresh schema';
  }
}

async function fetchConfigSchema(name) {
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/config`, {
      headers: authHeaders(),
      credentials: 'same-origin',
    });
    if (resp.status === 404) {
      document.getElementById('config-form-body').innerHTML =
        '<p style="color:#94a3b8;padding:1rem 0;">No configuration schema available for this component.</p>';
      document.querySelector('#config-modal .btn-primary').disabled = true;
      return;
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    _configSchema = data.schema || {};
    _configAssistCommand = data.config_assist_command || null;
    _configAssistSeeds   = data.config_assist_seeds   || [];

    // Fetch component URL suggestions in the background
    _fetchComponentSuggestions();

    // Drift detection
    const hasDrift = data.drift === true;
    if (hasDrift) {
      _showDriftBanner(name);
    } else {
      const existingBanner = document.getElementById('config-drift-banner');
      if (existingBanner) existingBanner.remove();
      document.querySelector('#config-modal .btn-primary').disabled = false;
    }

    generateConfigForm(_configSchema, data.current || {});
    _injectConfigAssistUI();

    // Re-apply drift-disabled state after form regeneration
    if (hasDrift) {
      document.querySelector('#config-modal .btn-primary').disabled = true;
      const fb = document.getElementById('config-form-body');
      if (fb) fb.querySelectorAll('input, select').forEach(el => { el.disabled = true; });
    }
  } catch (err) {
    showConfigModalError(err.message);
  }
}

async function _fetchComponentSuggestions() {
  try {
    const resp = await fetch('/components/suggest', {
      headers: authHeaders(),
      credentials: 'same-origin',
    });
    if (resp.ok) {
      const data = await resp.json();
      _componentSuggestions = data.components || [];
    }
  } catch {
    _componentSuggestions = [];
  }
}

function generateConfigForm(schema, current, containerOrId) {
  const container = typeof containerOrId === 'string'
    ? document.getElementById(containerOrId)
    : (containerOrId || document.getElementById('config-form-body'));
  container.innerHTML = '';
  _renderConfigNode(_ensureJsonSchema(schema), current, '', container);
}

// ── Legacy template support ─────────────────────────────────────────
// Components onboarded before the schema-driven config store a raw YAML
// template (plain values, "SECRET" sentinel) instead of a JSON Schema.
// Convert on the fly so the typed renderer and collector work for both.

function _ensureJsonSchema(schema) {
  if (schema && typeof schema === 'object' && schema.properties) return schema;
  return _legacyTemplateToSchema(schema || {});
}

function _legacyTemplateToSchema(template) {
  const properties = {};
  for (const [key, val] of Object.entries(template)) {
    properties[key] = _legacyValueToProp(val);
  }
  return { type: 'object', properties: properties };
}

function _legacyValueToProp(val) {
  if (val === 'SECRET') {
    return { type: 'string', format: 'password', writeOnly: true };
  }
  if (typeof val === 'boolean') return { type: 'boolean', default: val };
  if (typeof val === 'number') {
    return Number.isInteger(val)
      ? { type: 'integer', default: val }
      : { type: 'number', default: val };
  }
  if (Array.isArray(val)) return { type: 'array', default: val };
  if (val !== null && typeof val === 'object') return _legacyTemplateToSchema(val);
  return { type: 'string', default: val == null ? '' : val };
}

function _resolveRef(propSchema, defs) {
  if (!propSchema) return propSchema;
  // Unwrap a nullable union: anyOf/oneOf with exactly one non-null branch
  // (e.g. Optional[RepoConfig] → {anyOf: [{$ref}, {type: 'null'}]}). Without
  // this the field falls through to a text leaf and emits an invalid "" that
  // fails the schema's anyOf[object, null] on deploy. Recurse so a $ref branch
  // still resolves; preserve the wrapper's description/default if the inner
  // branch lacks them. Non-nullable or multi-branch unions are left unchanged.
  const union = propSchema.anyOf || propSchema.oneOf;
  if (Array.isArray(union)) {
    const nonNull = union.filter((b) => b && b.type !== 'null');
    if (nonNull.length === 1) {
      const inner = { ..._resolveRef(nonNull[0], defs) };
      if (inner.description === undefined && propSchema.description !== undefined) {
        inner.description = propSchema.description;
      }
      if (inner.default === undefined && propSchema.default !== undefined) {
        inner.default = propSchema.default;
      }
      return inner;
    }
    return propSchema;
  }
  if (!propSchema.$ref || !defs) return propSchema;
  const refPath = propSchema.$ref;
  if (refPath.startsWith('#/$defs/')) {
    const defName = refPath.slice('#/$defs/'.length);
    if (defs[defName]) return defs[defName];
  }
  return propSchema;
}

function _renderConfigNode(schema, current, prefix, container) {
  const properties = schema.properties;
  if (!properties) return;

  const required = schema.required || [];
  const defs = schema.$defs || {};

  for (const [key, propSchema] of Object.entries(properties)) {
    const fullKey = prefix ? prefix + '.' + key : key;
    const currentVal = (current != null) ? current[key] : undefined;

    const resolvedSchema = _resolveRef(propSchema, defs);

    const isRequired = required.includes(key);
    const defaultVal = resolvedSchema.default ?? currentVal ?? '';
    const isSecret = resolvedSchema.format === 'password' && resolvedSchema.writeOnly === true;

    if (resolvedSchema.type === 'object') {
      const section = document.createElement('div');
      section.className = 'env-section';
      const sectionDesc = resolvedSchema.description
        ? `<p style="margin:2px 0 8px;font-size:0.78rem;color:var(--muted);">${escHtml(resolvedSchema.description)}</p>`
        : '';
      section.innerHTML = `<h3>${escHtml(key)}</h3>${sectionDesc}`;
      const currentSub = (currentVal !== null && typeof currentVal === 'object'
                          && !Array.isArray(currentVal)) ? currentVal : {};
      _renderConfigNode(resolvedSchema, currentSub, fullKey, section);
      container.appendChild(section);
    } else {
      container.appendChild(buildConfigRow(
        fullKey, key, resolvedSchema, currentVal, isSecret, isRequired, defaultVal
      ));
    }
  }
}

function _renderArraySection(key, prefix, schemaArray, currentArray, container) {
  const itemTemplate = schemaArray[0] || {};
  // If no current values, seed with one blank item so the user has something to fill in
  const items = Array.isArray(currentArray) && currentArray.length > 0
    ? currentArray
    : [{}];

  const section = document.createElement('div');
  section.className = 'env-section array-section';
  section.dataset.arrayKey = prefix;
  section.innerHTML = `<h3>${escHtml(key)}</h3>`;

  const itemsContainer = document.createElement('div');
  itemsContainer.className = 'array-items-container';
  section.appendChild(itemsContainer);

  items.forEach((item, idx) => {
    _renderArrayItem(prefix, idx, itemTemplate, item, itemsContainer);
  });

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn-array-add';
  addBtn.textContent = `+ Add ${key} item`;
  addBtn.addEventListener('click', () => _addArrayItem(prefix, itemTemplate, itemsContainer));
  section.appendChild(addBtn);

  container.appendChild(section);
}

function _renderArrayItem(prefix, index, itemTemplate, itemCurrent, container) {
  const itemDiv = document.createElement('div');
  itemDiv.className = 'array-item';
  itemDiv.dataset.arrayIndex = index;
  itemDiv.dataset.arrayPrefix = prefix;  // e.g. "accounts"

  const heading = (itemCurrent && (itemCurrent.email || itemCurrent.id || itemCurrent.name))
                  || `[${index}]`;
  const headerDiv = document.createElement('div');
  headerDiv.className = 'array-item-header';
  headerDiv.innerHTML = `<span>${escHtml(String(heading))}</span>`;

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'btn-array-remove';
  removeBtn.textContent = 'Remove';
  removeBtn.addEventListener('click', () => {
    const itemsContainer = itemDiv.closest('.array-items-container');
    itemDiv.remove();
    if (itemsContainer) _reindexArrayItems(itemsContainer);
  });
  headerDiv.appendChild(removeBtn);
  itemDiv.appendChild(headerDiv);

  const body = document.createElement('div');
  body.className = 'array-item-body';
  _renderConfigNode(itemTemplate, itemCurrent || {}, `${prefix}.${index}`, body);
  itemDiv.appendChild(body);

  container.appendChild(itemDiv);
}

function _addArrayItem(prefix, itemTemplate, container) {
  const count = container.querySelectorAll(':scope > .array-item').length;
  _renderArrayItem(prefix, count, itemTemplate, {}, container);
}

function _reindexArrayItems(container) {
  const items = container.querySelectorAll(':scope > .array-item');
  items.forEach((itemDiv, newIdx) => {
    const oldIdx = parseInt(itemDiv.dataset.arrayIndex, 10);
    const prefix = itemDiv.dataset.arrayPrefix;
    if (oldIdx !== newIdx) {
      const oldSeg = `${prefix}.${oldIdx}.`;
      const newSeg = `${prefix}.${newIdx}.`;
      itemDiv.querySelectorAll('[data-key]').forEach(el => {
        if (el.dataset.key.startsWith(oldSeg)) {
          el.dataset.key = newSeg + el.dataset.key.slice(oldSeg.length);
        }
      });
    }
    itemDiv.dataset.arrayIndex = newIdx;
  });
}

function buildConfigRow(fullKey, labelKey, propSchema, currentVal, isSecret, isRequired, defaultVal) {
  const div = document.createElement('div');
  div.className = 'env-row';
  const displayVal = (currentVal !== undefined && currentVal !== null)
    ? currentVal
    : (defaultVal !== undefined && defaultVal !== null ? defaultVal : '');
  // Help bubble: the schema's field description when the model provides one,
  // with the dotted key path appended for orientation.
  const helpText = propSchema.description
    ? `${propSchema.description}\n(${fullKey})`
    : fullKey;

  let inputHtml;
  let urlSuggestHtml = '';
  if (isSecret) {
    const alreadySet = currentVal !== undefined && currentVal !== null && currentVal !== '';
    // One state, one message: an unset secret can be skipped now (onboard)
    // but the component needs it to run; a set secret is only replaced.
    const placeholder = alreadySet
      ? '(already set — enter new value to change)'
      : '(not set — can be saved later, needed to run)';
    inputHtml = `<input type="password" class="env-value" data-key="${escAttr(fullKey)}"
      value="" placeholder="${escAttr(placeholder)}" autocomplete="off">`;
    div.innerHTML = `
      <span class="env-key" title="${escAttr(helpText)}">${escHtml(labelKey)}${isRequired ? ' *' : ''}</span>
      ${inputHtml}
      <span class="env-badge" style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.75rem;background:rgba(251,146,60,0.18);color:var(--amber);">secret</span>
    `;
    return div;
  }

  if (propSchema.type === 'array' || Array.isArray(displayVal)) {
    // Arrays are edited as raw JSON — an invalid value is skipped on save
    // and the stored value is kept (prefer_existing_for_unset on the server).
    const jsonVal = JSON.stringify(displayVal === '' ? [] : displayVal);
    inputHtml = `<input type="text" class="env-value" data-key="${escAttr(fullKey)}"
      data-json="1" value="${escAttr(jsonVal)}" spellcheck="false">`;
    div.innerHTML = `
      <span class="env-key" title="${escAttr(helpText)}">${escHtml(labelKey)}${isRequired ? ' *' : ''}</span>
      ${inputHtml}
      <span style="font-size:0.75rem;color:var(--muted);margin-left:4px;">JSON list</span>
    `;
    return div;
  }

  if (propSchema.enum && Array.isArray(propSchema.enum)) {
    const selectedVal = (currentVal !== undefined && currentVal !== null)
      ? String(currentVal)
      : (defaultVal !== undefined && defaultVal !== null ? String(defaultVal) : '');
    const options = propSchema.enum.map(v => {
      const selected = String(v) === selectedVal;
      return `<option value="${escAttr(String(v))}"${selected ? ' selected' : ''}>${escHtml(String(v))}</option>`;
    }).join('');
    inputHtml = `<select class="env-value" data-key="${escAttr(fullKey)}">${options}</select>`;
  } else if (propSchema.type === 'integer' || propSchema.type === 'number') {
    const step = propSchema.type === 'integer' ? ' step="1"' : '';
    inputHtml = `<input type="number" class="env-value" data-key="${escAttr(fullKey)}"
      value="${escAttr(String(displayVal))}"${step}>`;
  } else if (propSchema.type === 'boolean') {
    const checked = displayVal === true || displayVal === 'true' || displayVal === 1 || displayVal === '1';
    inputHtml = `<input type="checkbox" class="env-value" data-key="${escAttr(fullKey)}"
      ${checked ? 'checked' : ''}>`;
  } else {
    if (/_url$/.test(labelKey) || /_base_url$/.test(labelKey)) {
      const prefix = labelKey.replace(/(_base)?_url$/, '');
      urlSuggestHtml = `<button type="button" class="btn-suggest" title="Suggest URL from peer components"
        data-suggest-for="${escAttr(fullKey)}" data-suggest-prefix="${escAttr(prefix)}">🔍</button>`;
    }
    inputHtml = `<input type="text" class="env-value" data-key="${escAttr(fullKey)}"
      value="${escAttr(String(displayVal))}">`;
  }

  div.innerHTML = `
    <span class="env-key" title="${escAttr(helpText)}">${escHtml(labelKey)}${isRequired ? ' *' : ''}</span>
    ${inputHtml}
    ${urlSuggestHtml || ''}
  `;
  return div;
}

/**
 * Set a value at a dotted path (e.g. "accounts.0.auth.username") inside obj,
 * creating intermediate objects/arrays as needed.
 */
function setNestedValue(obj, dotPath, value) {
  const parts = dotPath.split('.');
  for (let i = 0; i < parts.length - 1; i++) {
    const part = parts[i];
    const nextIsIndex = /^\d+$/.test(parts[i + 1]);
    if (obj[part] === undefined || obj[part] === null) {
      obj[part] = nextIsIndex ? [] : {};
    }
    obj = obj[part];
  }
  const last = parts[parts.length - 1];
  obj[last] = value;
}

function _collectFromProperties(schema, result, container, prefix) {
  const properties = schema.properties;
  if (!properties) return;

  const defs = schema.$defs || {};

  for (const [key, propSchema] of Object.entries(properties)) {
    const fullKey = prefix ? prefix + '.' + key : key;

    const resolvedSchema = _resolveRef(propSchema, defs);

    if (resolvedSchema.type === 'object') {
      const nestedResult = {};
      _collectFromProperties(resolvedSchema, nestedResult, container, fullKey);
      if (Object.keys(nestedResult).length > 0) {
        result[key] = nestedResult;
      }
    } else {
      const el = container.querySelector(`[data-key="${fullKey}"]`);
      if (!el) continue;

      const isSecret = resolvedSchema.format === 'password' && resolvedSchema.writeOnly === true;

      if (el.dataset.json === '1') {
        try {
          result[key] = JSON.parse(el.value);
        } catch (_) {
          // invalid JSON → omit; the server keeps the stored value
        }
      } else if (el.type === 'checkbox') {
        result[key] = el.checked;
      } else if (el.type === 'number') {
        result[key] = resolvedSchema.type === 'integer'
          ? parseInt(el.value, 10)
          : parseFloat(el.value);
      } else if (el.type === 'password' || isSecret) {
        result[key] = el.value === '' ? '***' : el.value;
      } else if (el.value === '') {
        // Omit an empty optional text leaf rather than storing "" — mirrors the
        // empty-nested-object omission above. Lets a nullable-object field whose
        // sub-fields are all blank collapse to an empty (→ omitted) object so the
        // assembled config validates (the field defaults to null).
        continue;
      } else {
        result[key] = el.value;
      }
    }
  }
}

function collectConfigValues(schema, containerEl) {
  const result = {};
  const root = containerEl || document.getElementById('config-form-body');
  _collectFromProperties(_ensureJsonSchema(schema), result, root, '');
  return result;
}

function _injectConfigAssistUI() {
  // Apply seed-field indicators (unchanged)
  for (const seed of _configAssistSeeds) {
    const el = document.querySelector(`#config-form-body [data-key="${escAttr(seed.key)}"]`);
    if (el) el.classList.add('seed-required');
  }
  // Remove existing bar/output (unchanged)
  const existingBar = document.getElementById('config-assist-bar');
  if (existingBar) existingBar.remove();
  const existingOut = document.getElementById('config-assist-output');
  if (existingOut) existingOut.remove();

  if (!_configAssistCommand) return;

  // Build seed inputs for each declared seed key.
  const seedInputsHtml = _configAssistSeeds.map(seed => {
    // Use declared label if present; otherwise derive from last non-numeric segment.
    const derivedLabel = seed.key.split('.').filter(s => !/^\d+$/.test(s)).pop() || seed.key;
    const displayLabel = seed.label || derivedLabel;
    const isSecret = /password|secret|token|key/i.test(seed.key);
    return `<label class="seed-label" style="font-size:0.85rem;margin-left:0.75rem;white-space:nowrap;">
      ${escHtml(displayLabel)}:
      <input id="seed-input-${escAttr(seed.key)}"
             type="${isSecret ? 'password' : 'text'}"
             placeholder="${escHtml(displayLabel)}"
             style="width:10rem;margin-left:0.25rem;"
             aria-label="Seed value for ${escHtml(seed.key)}" />
    </label>`;
  }).join('');

  const bar = document.createElement('div');
  bar.id = 'config-assist-bar';
  bar.style.cssText = 'display:flex;align-items:center;flex-wrap:wrap;gap:0.25rem 0;margin-top:0.5rem;';
  bar.innerHTML = `
    <button id="config-assist-btn" class="btn-secondary"
            onclick="runConfigAssist()">Auto-detect / Assist</button>
    ${seedInputsHtml}
    <span id="config-assist-spinner" style="display:none;margin-left:0.5rem;">&#x27F3; Running&hellip;</span>
  `;
  document.getElementById('config-form-body').appendChild(bar);

  const out = document.createElement('pre');
  out.id = 'config-assist-output';
  out.className = 'config-assist-output';
  out.style.display = 'none';
  document.getElementById('config-form-body').appendChild(out);
}

async function runConfigAssist() {
  if (!_configComponent || !_configSchema || !_configAssistCommand) return;
  const btn = document.getElementById('config-assist-btn');
  const spinner = document.getElementById('config-assist-spinner');
  const out = document.getElementById('config-assist-output');
  btn.disabled = true;
  spinner.style.display = 'inline';
  out.style.display = 'none';

  const values = collectConfigValues(_configSchema);

  // Merge seed bar inputs into values (so the email/password is included
  // even if the corresponding form section is not expanded).
  for (const seed of _configAssistSeeds) {
    const input = document.getElementById(`seed-input-${seed.key}`);
    if (input && input.value.trim() !== '') {
      setNestedValue(values, seed.key, input.value.trim());
    }
  }

  try {
    const resp = await fetch(`/services/${encodeURIComponent(_configComponent)}/config/assist`, {
      method: 'POST',
      headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
      body: JSON.stringify({ values }),
    });
    const data = await resp.json();
    out.style.display = 'block';
    if (!resp.ok) {
      out.textContent = `Error ${resp.status}: ${data.detail || JSON.stringify(data)}`;
    } else {
      out.textContent = data.output || '';
      generateConfigForm(_configSchema, data.config);  // re-render with auto-filled values
      _injectConfigAssistUI();                          // re-inject button + seed highlighting
    }
    // preserve output panel after re-inject
    const out2 = document.getElementById('config-assist-output');
    if (out2) {
      out2.style.display = 'block';
      out2.textContent = data.output || (data.detail ?? '');
    }
  } catch (err) {
    out.style.display = 'block';
    out.textContent = `Network error: ${err}`;
  } finally {
    const btn2 = document.getElementById('config-assist-btn');
    if (btn2) { btn2.disabled = false; }
    const sp2 = document.getElementById('config-assist-spinner');
    if (sp2) { sp2.style.display = 'none'; }
  }
}

async function saveConfigValues() {
  hideConfigModalError();
  if (!_configComponent || !_configSchema) return;

  let values;
  if (_configRawMode) {
    // Raw JSON mode: parse from textarea, validate against schema
    const rawText = document.getElementById('config-raw-body').value.trim();
    if (!rawText) {
      showConfigModalError('Config cannot be empty.');
      return;
    }
    try {
      values = JSON.parse(rawText);
    } catch (err) {
      showConfigModalError('Invalid JSON: ' + err.message);
      return;
    }
    // Validate required properties exist
    const schema = _ensureJsonSchema(_configSchema);
    if (schema && schema.properties) {
      const missing = (schema.required || []).filter(k => !(k in values));
      if (missing.length) {
        showConfigModalError('Missing required fields: ' + missing.join(', '));
        return;
      }
    }
  } else {
    values = collectConfigValues(_configSchema);
    // ^ uses the default container (#config-form-body)
  }

  // validate account id slugs before sending (form mode only; backend validates in raw mode)
  if (!_configRawMode) {
  const accountIdInputs = (document.getElementById('config-form-body')
    .querySelectorAll('[data-key]'));
  for (const input of accountIdInputs) {
    if (/^accounts\.\d+\.id$/.test(input.dataset.key)) {
      const val = input.value.trim();
      if (val && !/^[A-Za-z0-9._-]+$/.test(val)) {
        showConfigModalError(
          `Account id "${val}" must match ^[A-Za-z0-9._\\-]+$ ` +
          '(no @ or spaces — use a slug like "gmail" or "ovh")'
        );
        return;
      }
    }
  }
  }

  try {
    const resp = await fetch(`/services/${encodeURIComponent(_configComponent)}/config`, {
      method: 'PUT',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify({ values }),
    });
    if (resp.status === 204) {
      closeConfigModal();
    } else if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}));
      if (body.drift) {
        showConfigDriftConflict(body);
        return;
      }
      showConfigModalError(body.error || `HTTP 409`);
    } else {
      const body = await resp.json().catch(() => ({}));
      showConfigModalError(body.error || `HTTP ${resp.status}`);
    }
  } catch (err) {
    showConfigModalError(err.message);
  }
}

function showConfigModalError(msg) {
  const el = document.getElementById('config-modal-error');
  el.textContent = msg;
  el.style.display = 'block';
}

function hideConfigModalError() {
  document.getElementById('config-modal-error').style.display = 'none';
}

// ── Drift detection helpers ──────────────────────────────────

function _showDriftBanner(name) {
  // Remove any existing banner first
  const existing = document.getElementById('config-drift-banner');
  if (existing) existing.remove();

  const banner = document.createElement('div');
  banner.id = 'config-drift-banner';
  banner.style.cssText =
    'background: var(--amber-bg); color: var(--amber); ' +
    'border: 1px solid var(--amber); padding: 10px 14px; ' +
    'border-radius: 4px; margin-bottom: 12px; font-size: 0.82rem;';

  banner.innerHTML =
    '<strong>&#x26A0; Config was edited out-of-band since the last Save. ' +
    'The form shows the last saved values, not the current volume content.</strong>' +
    '<div style="margin-top:8px;display:flex;gap:8px;">' +
    '<button id="drift-import-btn" class="btn-secondary" style="font-size:0.78rem;">' +
    'Import volume &rarr; store</button>' +
    '<button id="drift-stale-btn" class="btn-secondary" style="font-size:0.78rem;">' +
    'Edit with stale values</button>' +
    '</div>';

  // Insert above the form body
  const formBody = document.getElementById('config-form-body');
  formBody.parentNode.insertBefore(banner, formBody);

  // Disable Save + form inputs while banner is visible
  document.querySelector('#config-modal .btn-primary').disabled = true;
  formBody.querySelectorAll('input, select').forEach(el => { el.disabled = true; });

  // Wire buttons
  document.getElementById('drift-import-btn').addEventListener('click', () => {
    importConfigFromVolume(name);
  });
  document.getElementById('drift-stale-btn').addEventListener('click', () => {
    // Hide banner, enable form
    const b = document.getElementById('config-drift-banner');
    if (b) b.remove();
    document.querySelector('#config-modal .btn-primary').disabled = false;
    const fb = document.getElementById('config-form-body');
    if (fb) fb.querySelectorAll('input, select').forEach(el => { el.disabled = false; });
  });
}

async function importConfigFromVolume(name) {
  try {
    const resp = await fetch(
      `/services/${encodeURIComponent(name)}/config/import`,
      { method: 'POST', headers: authHeaders(), credentials: 'same-origin' }
    );
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showConfigModalError(err.detail || `Import failed: HTTP ${resp.status}`);
      return;
    }
    // Remove conflict panel if present
    const conflictPanel = document.getElementById('config-drift-conflict');
    if (conflictPanel) conflictPanel.remove();
    // Restore form body visibility
    const formBody = document.getElementById('config-form-body');
    if (formBody) formBody.style.display = '';
    // Re-fetch config form (clears drift banner, loads imported values)
    await fetchConfigSchema(name);
  } catch (e) {
    showConfigModalError(e.message);
  }
}

function showConfigDriftConflict(body) {
  // Remove any existing conflict panel
  const existing = document.getElementById('config-drift-conflict');
  if (existing) existing.remove();

  // Hide regular form body
  const formBody = document.getElementById('config-form-body');
  formBody.style.display = 'none';

  // Hide drift banner if present
  const banner = document.getElementById('config-drift-banner');
  if (banner) banner.style.display = 'none';

  const panel = document.createElement('div');
  panel.id = 'config-drift-conflict';
  panel.innerHTML =
    '<h3 style="color:var(--amber);margin-bottom:8px;">' +
    'Save blocked &mdash; config edited out-of-band</h3>' +
    renderConfigDiff(body.live_config, body.stored_config) +
    '<div style="margin-top:12px;display:flex;gap:8px;">' +
    '<button id="drift-conflict-import-btn" class="btn-primary" style="font-size:0.78rem;">' +
    'Import and re-edit</button>' +
    '<button id="drift-conflict-overwrite-btn" class="btn-danger" style="font-size:0.78rem;">' +
    'Overwrite (destructive)</button>' +
    '<button id="drift-conflict-cancel-btn" class="btn-secondary" style="font-size:0.78rem;">' +
    'Cancel</button>' +
    '</div>';

  // Insert after the form body (before the footer)
  formBody.parentNode.insertBefore(panel, formBody.nextSibling);

  // Wire buttons
  document.getElementById('drift-conflict-import-btn').addEventListener('click', () => {
    importConfigFromVolume(_configComponent);
  });
  document.getElementById('drift-conflict-overwrite-btn').addEventListener('click', () => {
    confirmAndOverwrite();
  });
  document.getElementById('drift-conflict-cancel-btn').addEventListener('click', () => {
    closeConfigModal();
  });
}

function confirmAndOverwrite() {
  if (!window.confirm(
    'This will overwrite the volume with your form values, ' +
    'discarding the out-of-band changes. Continue?'
  )) return;

  if (!_configComponent || !_configSchema) return;
  const values = collectConfigValues(_configSchema);

  fetch(`/services/${encodeURIComponent(_configComponent)}/config`, {
    method: 'PUT',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    credentials: 'same-origin',
    body: JSON.stringify({ values, force_overwrite: true }),
  }).then(async resp => {
    if (resp.status === 204) {
      closeConfigModal();
    } else {
      const errBody = await resp.json().catch(() => ({}));
      showConfigModalError(errBody.error || `HTTP ${resp.status}`);
    }
  }).catch(err => {
    showConfigModalError(err.message);
  });
}

function renderConfigDiff(liveConfig, storedConfig) {
  if (!liveConfig && !storedConfig) {
    return '<p style="color:#94a3b8;padding:0.5rem 0;">No differing keys found.</p>';
  }

  // Flatten both objects to dot-joined keys (one level of recursion)
  const flatten = (obj, prefix) => {
    const result = {};
    if (obj == null || typeof obj !== 'object') return result;
    for (const [k, v] of Object.entries(obj)) {
      const fullKey = prefix ? prefix + '.' + k : k;
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
        Object.assign(result, flatten(v, fullKey));
      } else {
        result[fullKey] = v;
      }
    }
    return result;
  };

  const flatLive = flatten(liveConfig || {}, '');
  const flatStored = flatten(storedConfig || {}, '');
  const allKeys = [...new Set([...Object.keys(flatLive), ...Object.keys(flatStored)])].sort();

  // Collect only differing keys
  const diffRows = [];
  for (const key of allKeys) {
    const lv = flatLive[key];
    const sv = flatStored[key];
    if (lv === sv) continue;
    diffRows.push({ key, live: lv, stored: sv });
  }

  if (diffRows.length === 0) {
    return '<p style="color:#94a3b8;padding:0.5rem 0;">No differing keys found.</p>';
  }

  const truncate = (v) => {
    if (v === undefined || v === null) return String(v ?? '');
    const s = String(v);
    return s.length > 60 ? s.slice(0, 60) + '\u2026' : s;
  };

  const fmtVal = (v) => {
    if (v === '***') return '***';
    return escHtml(truncate(v));
  };

  let html =
    '<table style="width:100%;font-size:0.82rem;margin-top:8px;">' +
    '<thead><tr>' +
    '<th style="background:#243044;">Field</th>' +
    '<th style="background:#243044;">In volume (live)</th>' +
    '<th style="background:#243044;">Your form (stored)</th>' +
    '</tr></thead><tbody>';

  for (const row of diffRows) {
    const bothSecret = row.live === '***' || row.stored === '***';
    const liveDisplay = bothSecret ? '***' : fmtVal(row.live);
    const storedDisplay = bothSecret ? '***' : fmtVal(row.stored);
    html +=
      '<tr style="background:rgba(251,146,60,0.08);">' +
      '<td style="border-bottom:1px solid #334155;padding:6px 8px;">' +
      escHtml(row.key) + '</td>' +
      '<td style="border-bottom:1px solid #334155;padding:6px 8px;">' +
      liveDisplay + '</td>' +
      '<td style="border-bottom:1px solid #334155;padding:6px 8px;">' +
      storedDisplay + '</td>' +
      '</tr>';
  }

  html += '</tbody></table>';
  return html;
}

// ── History modal ──────────────────────────────────────────────

let _historyModalName = null;

function openHistoryModal(name) {
  _historyModalName = name;
  document.getElementById('history-modal-component').textContent = name;
  document.getElementById('history-modal-error').style.display = 'none';
  document.getElementById('history-tbody').innerHTML =
    '<tr><td colspan="5" style="text-align:center;padding:2rem;color:#94a3b8;">Loading…</td></tr>';
  document.getElementById('history-modal').classList.add('open');
  fetchHistory(name);
}

function closeHistoryModal() {
  document.getElementById('history-modal').classList.remove('open');
  _historyModalName = null;
}

function showHistoryError(msg) {
  const el = document.getElementById('history-modal-error');
  el.textContent = msg;
  el.style.display = 'block';
}

async function fetchHistory(name) {
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/history`, {
      credentials: 'same-origin',
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    renderHistoryRows(data.entries || [], data.running_digest || '');
  } catch (err) {
    showHistoryError('Failed to load history: ' + err.message);
    document.getElementById('history-tbody').innerHTML =
      '<tr><td colspan="5" style="text-align:center;padding:2rem;color:#f87171;">Failed to load history.</td></tr>';
  }
}

function renderHistoryRows(entries, runningDigest) {
  const tbody = document.getElementById('history-tbody');
  if (!entries || entries.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="5" style="text-align:center;padding:2rem;color:#94a3b8;">No deploy history recorded yet.</td></tr>';
    return;
  }

  let html = '';
  for (const entry of entries) {
    const digest = entry.digest || '';
    const shortDigest = digest.replace(/^sha256:/, '').slice(0, 12) || '\u2014';
    const isCurrent = runningDigest && digest === runningDigest;
    const rowClass = isCurrent ? 'current-row' : '';
    const ts = entry.recorded_at
      ? new Date(entry.recorded_at * 1000).toLocaleString()
      : '\u2014';
    const source = entry.source || '';
    const sourceBadge = source
      ? `<span class="badge badge-${source === 'manual' ? 'update-ok' : source === 'rollback' ? 'update-avail' : 'update-unknown'}">${escHtml(source)}</span>`
      : '\u2014';
    const imageRef = entry.image_ref || '\u2014';
    const rollbackBtn = isCurrent
      ? '<span style="font-size:0.78rem;color:var(--blue);">current</span>'
      : `<button style="font-size:0.75rem;padding:3px 8px;" onclick="rollbackTo('${escAttr(_historyModalName)}', '${escAttr(digest)}')">Rollback</button>`;

    html += `<tr class="${rowClass}">
      <td><span title="${escAttr(digest)}">${escHtml(shortDigest)}</span></td>
      <td>${escHtml(ts)}</td>
      <td>${sourceBadge}</td>
      <td>${escHtml(imageRef)}</td>
      <td>${rollbackBtn}</td>
    </tr>`;
  }
  tbody.innerHTML = html;
}

async function rollbackTo(name, digest) {
  if (!window.confirm(`Roll back "${name}" to digest ${digest.replace(/^sha256:/, '').slice(0, 12)}?\n\nThis will recreate the container using the previously-recorded image.`)) return;
  const tbody = document.getElementById('history-tbody');
  tbody.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const resp = await fetch(`/services/${encodeURIComponent(name)}/rollback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ digest }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
    }
    // Refresh the service row
    const svc = await fetchOneStatus(name);
    const rowEl = document.getElementById(`row-${name}`);
    if (rowEl) rowEl.outerHTML = renderRow(svc);
    updateRefreshTime();
    // Refresh the history modal
    await fetchHistory(name);
  } catch (err) {
    showHistoryError('Rollback failed: ' + err.message);
    tbody.querySelectorAll('button').forEach(b => b.disabled = false);
  }
}

function escHtml(s) {
  const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  return String(s).replace(/[&<>"']/g, ch => map[ch]);
}

function escAttr(s) {
  return String(s).replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
}

// ── Onboard modal ──────────────────────────────────────────────

let _obSpec = null;
let _obPortShifts = [];
let _obJobPollTimer = null;

function openOnboardModal() {
  resetOnboardModal();
  document.getElementById('onboard-modal').classList.add('open');
}

function closeOnboardModal() {
  stopOnboardJobPoll();
  document.getElementById('onboard-modal').classList.remove('open');
  resetOnboardModal();
}

function resetOnboardModal() {
  _obSpec = null;
  _obPortShifts = [];
  document.getElementById('ob-git-url').value = '';
  document.getElementById('ob-name').value = '';
  var progEl = document.getElementById('ob-deploy-progress');
  progEl.style.display = 'none';
  progEl.innerHTML = '';
  showStep1();
}

function showStep1() {
  document.getElementById('onboard-step1').style.display = '';
  document.getElementById('onboard-step2').style.display = 'none';
  var e = document.getElementById('ob-step1-error');
  e.style.display = 'none';
  e.innerHTML = '';
}

function showOnboardError(el, status, data) {
  var html = '';
  if (status === 409) {
    html = '<p>Conflict: ' + escHtml(data.error || 'Component already exists.') + '</p>';
  } else if (data.violations && data.violations.length) {
    html = '<ul>' + data.violations.map(function(v) { return '<li>' + escHtml(v) + '</li>'; }).join('') + '</ul>';
  } else {
    html = '<p>' + escHtml(data.error || 'Unexpected error.') + '</p>';
  }
  el.innerHTML = html;
  el.style.display = '';
}

var ONBOARD_PHASE_LABELS = {
  writing_config: 'Writing config\u2026',
  deploying_primary: 'Deploying primary\u2026',
  waiting_health: 'Waiting for health check\u2026',
  deploying_siblings: 'Deploying siblings\u2026',
  done: 'Done',
  failed: 'Failed',
};

function setOnboardInputsDisabled(disabled) {
  document.getElementById('ob-deploy-btn').disabled = disabled;
  document.getElementById('ob-back-btn').disabled = disabled;
  document.getElementById('ob-cancel-btn').disabled = disabled;
  document.getElementById('ob-close-x').disabled = disabled;
  document.querySelectorAll('.ob-env-val,.ob-port-host,#ob-claude-mount').forEach(function(el) {
    el.disabled = disabled;
  });
  var cfgBody = document.getElementById('ob-config-form-body');
  if (cfgBody) {
    cfgBody.querySelectorAll('input,select,textarea,button').forEach(function(el) {
      el.disabled = disabled;
    });
  }
}

function stopOnboardJobPoll() {
  if (_obJobPollTimer !== null) {
    clearTimeout(_obJobPollTimer);
    _obJobPollTimer = null;
  }
}

function pollOnboardJob(jobId, errEl) {
  stopOnboardJobPoll();

  var progEl = document.getElementById('ob-deploy-progress');

  function poll() {
    _obJobPollTimer = setTimeout(async function() {
      try {
        var res = await fetch('/onboard/jobs/' + encodeURIComponent(jobId), {
          headers: authHeaders(),
          credentials: 'same-origin',
        });
        if (res.status === 404) {
          stopOnboardJobPoll();
          showOnboardError(errEl, 404, { error: 'Job not found \u2014 server may have restarted.' });
          setOnboardInputsDisabled(false);
          document.getElementById('ob-deploy-btn').textContent = 'Deploy';
          progEl.style.display = 'none';
          return;
        }
        if (!res.ok) {
          stopOnboardJobPoll();
          var errData = await res.json().catch(function() { return {}; });
          showOnboardError(errEl, res.status, errData);
          setOnboardInputsDisabled(false);
          document.getElementById('ob-deploy-btn').textContent = 'Deploy';
          progEl.style.display = 'none';
          return;
        }
        var job = await res.json();
        var label = ONBOARD_PHASE_LABELS[job.phase] || job.phase;
        progEl.innerHTML = escHtml(label);
        progEl.style.display = '';

        if (job.phase === 'done') {
          if (job.warnings && job.warnings.length > 0) {
            var warnHtml = '<div style="background:#fff3cd;border:1px solid #ffc107;padding:8px 12px;'
              + 'border-radius:4px;margin-top:8px;">'
              + '\u26a0\ufe0f <strong>Port-shift notifications not sent \u2014 mill was unreachable:</strong>'
              + '<ul style="margin:4px 0 0 16px">' + job.warnings.map(function(w) {
                  return '<li>' + escHtml(w) + '</li>';
                }).join('') + '</ul>'
              + '<small>Please manually notify the affected component maintainers to update their deploy/docker-compose.yml.</small>'
              + '<br><button onclick="this.parentElement.remove();closeOnboardModal();loadDashboard();">OK</button>'
              + '</div>';
            progEl.innerHTML = warnHtml;
            progEl.style.display = '';
            stopOnboardJobPoll();
            // closeOnboardModal() and loadDashboard() are deferred to the OK button above
            return;
          }
          stopOnboardJobPoll();
          closeOnboardModal();
          loadDashboard();
        } else if (job.phase === 'failed') {
          stopOnboardJobPoll();
          showOnboardError(errEl, 0, { error: job.error || 'Deploy failed.' });
          setOnboardInputsDisabled(false);
          document.getElementById('ob-deploy-btn').textContent = 'Deploy';
          progEl.style.display = 'none';
        } else {
          // still in progress — keep polling
          poll();
        }
      } catch (e) {
        stopOnboardJobPoll();
        showOnboardError(errEl, 0, { error: 'Network error \u2014 check server logs.' });
        setOnboardInputsDisabled(false);
        document.getElementById('ob-deploy-btn').textContent = 'Deploy';
        progEl.style.display = 'none';
      }
    }, 1500);
  }

  poll();
}

async function onboardFetch() {
  var gitUrl = document.getElementById('ob-git-url').value.trim();
  var name   = document.getElementById('ob-name').value.trim();
  var errEl  = document.getElementById('ob-step1-error');
  errEl.style.display = 'none';

  var btn = document.getElementById('ob-fetch-btn');
  btn.disabled = true;
  btn.textContent = 'Fetching…';

  try {
    var res = await fetch('/onboard/preflight', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify({ git_url: gitUrl, name: name }),
    });
    var data = await res.json();
    if (!res.ok) {
      showOnboardError(errEl, res.status, data);
      return;
    }
    _obSpec = data.spec;
    _obPortShifts = data.port_shifts || [];
    populateStep2(_obSpec, _obPortShifts);
    document.getElementById('onboard-step1').style.display = 'none';
    document.getElementById('onboard-step2').style.display = '';
  } catch (e) {
    showOnboardError(errEl, 0, { error: 'Network error — check server logs.' });
  } finally {
    btn.disabled = false;
    btn.textContent = 'Fetch Spec';
  }
}

function populateStep2(spec, portShifts) {
  portShifts = portShifts || [];
  document.getElementById('ob-image').textContent = spec.image;

  // Ports
  var portsEl = document.getElementById('ob-ports');
  var shiftBanner = '';
  if (portShifts.length > 0) {
    var items = portShifts.map(function(s) {
      return '<li>Port <em>' + escHtml(String(s.container_port)) + '/' + escHtml(s.protocol) + '</em>: '
        + 'default ' + escHtml(String(s.original_host)) + ' \u2192 auto-assigned '
        + escHtml(String(s.assigned_host))
        + (s.collision_component_id ? ' (collides with \u2018' + escHtml(s.collision_component_id) + '\u2019)' : '')
        + '</li>';
    }).join('');
    shiftBanner = '<div style="background:#fff3cd;border:1px solid #ffc107;padding:8px 12px;'
      + 'border-radius:4px;margin-bottom:8px;">'
      + '\u26a0\ufe0f <strong>Port defaults adjusted:</strong><ul style="margin:4px 0 0 16px">'
      + items + '</ul>'
      + '<small>Host ports were auto-assigned to avoid collisions with existing components. '
      + 'You may override them below. Affected component maintainers will be notified via the mill (if reachable).</small>'
      + '</div>';
  }
  portsEl.innerHTML = shiftBanner + spec.ports.map(function(p, i) {
    return '<div><label>Port ' + (i + 1) + ': <input class="ob-port-host" data-idx="' + i + '" type="number" value="' + escAttr(String(p.host)) + '" style="width:80px" /> → ' + escHtml(String(p.container)) + '/' + escHtml(p.protocol) + '</label></div>';
  }).join('');

  // Volumes
  var volsEl = document.getElementById('ob-volumes');
  // No "starts EMPTY" badge here: onboarding creates a brand-new
  // component, so every volume legitimately starts empty. Backups are
  // the operator's responsibility.
  volsEl.innerHTML = spec.volume_mounts.map(function(m) {
    return '<div>📦 <strong>' + escHtml(m.host) + '</strong> → ' + escHtml(m.container) + '</div>';
  }).join('');

  // Env — show only for repos WITHOUT config.yaml
  // (legacy raw templates are converted by _ensureJsonSchema)
  var hasConfig = spec.config_schema
      && Object.keys(_ensureJsonSchema(spec.config_schema).properties).length > 0;
  var envTable = document.getElementById('ob-env-table');
  var configSection = document.getElementById('ob-config-section');

  if (hasConfig) {
    // Hide env table; show the full config.yaml mirror
    envTable.style.display = 'none';
    configSection.style.display = '';
    generateConfigForm(spec.config_schema, {}, 'ob-config-form-body');
  } else {
    // Show env table; hide config section
    envTable.style.display = '';
    configSection.style.display = 'none';
    var tbody = document.getElementById('ob-env-body');
    tbody.innerHTML = Object.entries(spec.env).map(function(entry) {
      var k = entry[0], v = entry[1];
      return '<tr><td>' + escHtml(k) + '</td><td><input class="ob-env-val" data-key="' + escAttr(k) + '" type="' + (v === '' ? 'password' : 'text') + '" value="' + escAttr(v) + '" placeholder="' + (v === '' ? '(secret — can be set later)' : '') + '" /></td></tr>';
    }).join('');
  }

  // Claude mount toggle
  document.getElementById('ob-claude-mount').checked = !!spec.claude_mount;

  // Chat access toggle
  document.getElementById('ob-chat-access').checked = !!spec.allow_chat_access;
}

async function onboardDeploy() {
  var errEl = document.getElementById('ob-step2-error');
  errEl.style.display = 'none';
  var progEl = document.getElementById('ob-deploy-progress');
  progEl.style.display = 'none';
  progEl.innerHTML = '';

  // Deep clone the spec
  var finalSpec = JSON.parse(JSON.stringify(_obSpec));

  // Env values (only for repos without config.yaml)
  document.querySelectorAll('.ob-env-val').forEach(function(input) {
    finalSpec.env[input.dataset.key] = input.value;
  });

  // Port host overrides
  document.querySelectorAll('.ob-port-host').forEach(function(input) {
    finalSpec.ports[parseInt(input.dataset.idx)].host = parseInt(input.value);
  });

  // Claude mount toggle
  finalSpec.claude_mount = document.getElementById('ob-claude-mount').checked;

  // Chat access toggle
  finalSpec.allow_chat_access = document.getElementById('ob-chat-access').checked;

  // Build the confirm body
  var body = { spec: finalSpec };

  // Mill tracking opt-in
  body.register_with_mill = document.getElementById('ob-register-mill').checked;

  // Echo port shifts from preflight
  body.port_shifts = _obPortShifts;

  // When config.yaml schema is present, collect the filled-in values
  if (finalSpec.config_schema
      && Object.keys(_ensureJsonSchema(finalSpec.config_schema).properties).length > 0) {
    var configFormContainer = document.getElementById('ob-config-form-body');
    body.config_values = collectConfigValues(finalSpec.config_schema, configFormContainer);
  }

  var btn = document.getElementById('ob-deploy-btn');
  setOnboardInputsDisabled(true);
  btn.textContent = 'Deploying…';

  try {
    var res = await fetch('/onboard/confirm', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify(body),
    });
    var data = await res.json();
    if (!res.ok) {
      showOnboardError(errEl, res.status, data);
      setOnboardInputsDisabled(false);
      btn.textContent = 'Deploy';
      return;
    }
    // 202 Accepted — start polling the job
    pollOnboardJob(data.job_id, errEl);
  } catch (e) {
    showOnboardError(errEl, 0, { error: 'Network error — check server logs.' });
    setOnboardInputsDisabled(false);
    btn.textContent = 'Deploy';
  }
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(loadDashboard, REFRESH_INTERVAL_MS);
}

async function doLogout() {
  try {
    const resp = await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
    if (resp.redirected || resp.ok) {
      window.location.href = '/login';
    } else {
      window.location.href = '/login';
    }
  } catch (_) {
    window.location.href = '/login';
  }
}

// ── Settings section ─────────────────────────────────────────────

function showSettingsSection() {
  document.querySelector('header').style.display = 'none';
  document.querySelectorAll('body > .disk-panel').forEach(el => el.style.display = 'none');
  document.querySelector('table').style.display = 'none';
  document.getElementById('settings-section').style.display = '';
  loadSettings();
  fetchClaudeAuthStatus();
}

function hideSettingsSection() {
  document.getElementById('settings-section').style.display = 'none';
  document.querySelector('header').style.display = '';
  document.querySelectorAll('body > .disk-panel').forEach(el => el.style.display = '');
  document.querySelector('table').style.display = '';
}

function changeSecret(inputId) {
  var inp = document.getElementById(inputId);
  inp.disabled = false;
  inp.value = '';
  inp.placeholder = '';
  inp.focus();
}

function showToast(msg, type) {
  var el = document.getElementById('settings-toast');
  el.textContent = msg;
  el.style.color = type === 'error' ? 'var(--red)' : 'var(--green)';
  setTimeout(function() { el.textContent = ''; }, 4000);
}

async function loadSettings() {
  try {
    var res = await fetch('/settings', { headers: authHeaders(), credentials: 'same-origin' });
    if (!res.ok) { showToast('Error loading settings: HTTP ' + res.status, 'error'); return; }
    var s = await res.json();
    document.getElementById('s-disk-warn').value = s.disk_warn_pct;
    document.getElementById('s-reg-interval').value = s.registry_check_interval;
    document.getElementById('s-log-level').value = s.log_level;
    document.getElementById('s-gateway-domain').value = s.gateway_base_domain || '';
    document.getElementById('s-caretaker-enabled').checked = !!s.caretaker_enabled;
    document.getElementById('s-caretaker-interval').value = s.caretaker_interval_hours || 24;
    document.getElementById('s-mill-component').value = s.mill_component_id || 'mill';
    document.getElementById('s-image-prune').checked = !!s.image_auto_prune;
    document.getElementById('s-caretaker-interval-row').style.display = s.caretaker_enabled ? '' : 'none';
    document.getElementById('s-mill-component-row').style.display = s.caretaker_enabled ? '' : 'none';
    // LLMIO tier config
    if (s.llmio_tier_config) {
      for (var level = 1; level <= 4; level++) {
        var key = 'level' + level;
        var entry = s.llmio_tier_config[key] || {};
        var provEl = document.getElementById('tier-' + key + '-provider');
        var modelEl = document.getElementById('tier-' + key + '-model');
        if (provEl) provEl.value = entry.provider || '';
        if (modelEl) modelEl.value = entry.model || '';
      }
    }
    // Also update module-level variable used for service shortcut links
    gatewayBaseDomain = s.gateway_base_domain || '';
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function saveSettings(event) {
  event.preventDefault();
  var body = {
    disk_warn_pct: parseFloat(document.getElementById('s-disk-warn').value),
    registry_check_interval: parseInt(document.getElementById('s-reg-interval').value),
    log_level: document.getElementById('s-log-level').value,
    gateway_base_domain: document.getElementById('s-gateway-domain').value,
    caretaker_enabled: document.getElementById('s-caretaker-enabled').checked,
    caretaker_interval_hours: parseInt(document.getElementById('s-caretaker-interval').value),
    mill_component_id: document.getElementById('s-mill-component').value.trim() || 'mill',
    image_auto_prune: document.getElementById('s-image-prune').checked,
  };
  // Build llmio_tier_config from the form fields
  var tierConfig = {};
  for (var level = 1; level <= 4; level++) {
    var key = 'level' + level;
    var provider = document.getElementById('tier-' + key + '-provider').value.trim();
    var model = document.getElementById('tier-' + key + '-model').value.trim();
    if (provider || model) {
      tierConfig[key] = { provider: provider, model: model };
    }
  }
  body.llmio_tier_config = tierConfig;
  try {
    var res = await fetch('/settings', {
      method: 'PUT',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify(body),
    });
    if (res.ok) {
      showToast('Settings saved');
      // Reload to refresh masked fields
      loadSettings();
    } else {
      var data = await res.json().catch(function() { return {}; });
      showToast('Error: ' + (data.error || 'HTTP ' + res.status), 'error');
    }
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

function onCaretakerEnabledChange() {
  var enabled = document.getElementById('s-caretaker-enabled').checked;
  document.getElementById('s-caretaker-interval-row').style.display = enabled ? '' : 'none';
  document.getElementById('s-mill-component-row').style.display = enabled ? '' : 'none';
}

async function checkCaretakerStatus() {
  try {
    var res = await fetch('/caretaker/status', { headers: authHeaders(), credentials: 'same-origin' });
    if (!res.ok) return;
    var data = await res.json();
    var banner = document.getElementById('caretaker-degraded-banner');
    if (data.enabled && !data.mill_reachable) {
      banner.style.display = 'block';
    } else {
      banner.style.display = 'none';
    }
  } catch (_) { /* transient */ }
}

// ── Volume browser ────────────────────────────────────────────────

let currentVolumeName = null;
let currentVolumePath = '';

function openVolumeBrowser(name) {
  currentVolumeName = name;
  currentVolumePath = '';
  document.getElementById('vb-volume-name').textContent = name;
  document.getElementById('vb-error').classList.remove('open');
  document.getElementById('vb-content-area').classList.remove('open');
  document.getElementById('vb-content-area').innerHTML = '';
  document.getElementById('vb-modal').classList.add('open');
  loadVolumeDir('');
}

function closeVolumeBrowser() {
  document.getElementById('vb-modal').classList.remove('open');
  currentVolumeName = null;
  currentVolumePath = '';
}

async function loadVolumeDir(path) {
  const errorEl = document.getElementById('vb-error');
  const listingEl = document.getElementById('vb-listing');
  const breadcrumbEl = document.getElementById('vb-breadcrumb');
  const contentArea = document.getElementById('vb-content-area');
  errorEl.classList.remove('open');
  contentArea.classList.remove('open');
  contentArea.innerHTML = '';
  listingEl.innerHTML = '<div style="color:#64748b;font-size:0.82rem;">Loading\u2026</div>';

  try {
    const resp = await fetch(
      `/volumes/${encodeURIComponent(currentVolumeName)}/ls?path=${encodeURIComponent(path || '/')}`,
      { headers: authHeaders(), credentials: 'same-origin' }
    );
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.error || data.detail || `HTTP ${resp.status}`;
      if (resp.status === 404) {
        showVbError('This volume is not browsable');
      } else {
        showVbError(detail);
      }
      listingEl.innerHTML = '';
      return;
    }

    currentVolumePath = data.path || path;
    renderBreadcrumb(currentVolumePath);
    renderVolumeListing(data.entries || []);
  } catch (e) {
    showVbError(e.message);
    listingEl.innerHTML = '';
  }
}

function renderBreadcrumb(path) {
  const el = document.getElementById('vb-breadcrumb');
  const parts = (path || '').split('/').filter(Boolean);
  let html = '<button onclick="loadVolumeDir(\'\')">/ (root)</button>';
  let accum = '';
  for (const p of parts) {
    accum += '/' + p;
    html += ` <span style="color:#475569;">/</span> <button onclick="loadVolumeDir('${escAttr(accum)}')">${escHtml(p)}</button>`;
  }
  el.innerHTML = html;
}

function renderVolumeListing(entries) {
  const el = document.getElementById('vb-listing');
  if (!entries || entries.length === 0) {
    el.innerHTML = '<div style="color:#64748b;font-size:0.82rem;">(empty directory)</div>';
    return;
  }

  // Show parent link if not at root
  let parentRow = '';
  if (currentVolumePath && currentVolumePath !== '' && currentVolumePath !== '/') {
    const parentPath = currentVolumePath.substring(0, currentVolumePath.lastIndexOf('/')) || '';
    parentRow = `<tr><td colspan="2"><span class="vb-parent" onclick="loadVolumeDir('${escAttr(parentPath || '')}')">\u2190 .. (parent)</span></td></tr>`;
  }

  const sorted = [...entries].sort((a, b) => {
    if (a.type === b.type) return a.name.localeCompare(b.name);
    return a.type === 'dir' ? -1 : 1;
  });

  const rows = sorted.map(e => {
    const name = escHtml(e.name);
    const size = e.size_bytes !== undefined ? fmt_bytes(e.size_bytes) : '\u2014';
    const fullPath = (currentVolumePath ? currentVolumePath + '/' : '') + e.name;
    if (e.type === 'dir') {
      return `<tr><td><span class="vb-entry vb-dir" onclick="loadVolumeDir('${escAttr(fullPath)}')">${name}</span></td><td style="color:#64748b;">\u2014</td></tr>`;
    }
    return `<tr><td><span class="vb-entry vb-file" onclick="loadVolumeFile('${escAttr(fullPath)}', '${escAttr(e.name)}')">${name}</span></td><td style="color:#94a3b8;">${size}</td></tr>`;
  }).join('');

  el.innerHTML = `<table>${parentRow}${rows}</table>`;
}

async function loadVolumeFile(filePath, displayName) {
  const errorEl = document.getElementById('vb-error');
  const contentArea = document.getElementById('vb-content-area');
  errorEl.classList.remove('open');

  try {
    const resp = await fetch(
      `/volumes/${encodeURIComponent(currentVolumeName)}/cat?path=${encodeURIComponent(filePath)}`,
      { headers: authHeaders(), credentials: 'same-origin' }
    );
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data.error || data.detail || `HTTP ${resp.status}`;
      showVbError(detail);
      return;
    }

    let metaHtml = `<span>${escHtml(displayName || filePath)}</span>`;
    if (data.size_bytes !== undefined) {
      metaHtml += ` <span style="color:#64748b;">(${fmt_bytes(data.size_bytes)})</span>`;
    }
    if (data.truncated) {
      metaHtml += ' <span style="color:#f59e0b;">[truncated \u2014 showing first ' + escHtml(String(data.size_bytes || 'N')) + ' bytes]</span>';
    }

    if (data.binary) {
      contentArea.innerHTML = `<div class="vb-content-meta">${metaHtml}</div><pre style="color:#f59e0b;">Binary file \u2014 not displayed</pre>`;
    } else {
      contentArea.innerHTML = `<div class="vb-content-meta">${metaHtml}</div><pre>${escHtml(data.content || '')}</pre>`;
    }
    contentArea.classList.add('open');
  } catch (e) {
    showVbError(e.message);
  }
}

function showVbError(msg) {
  const el = document.getElementById('vb-error');
  el.textContent = msg;
  el.classList.add('open');
}

// ── Config suggest dropdown ─────────────────────────────────────

let _activeSuggestDropdown = null;

function _closeSuggestDropdown() {
  if (_activeSuggestDropdown) {
    _activeSuggestDropdown.remove();
    _activeSuggestDropdown = null;
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.btn-suggest');
  if (btn) {
    e.stopPropagation();
    _closeSuggestDropdown();
    _showSuggestDropdown(btn);
    return;
  }
  // Close dropdown when clicking outside
  if (_activeSuggestDropdown && !_activeSuggestDropdown.contains(e.target)) {
    _closeSuggestDropdown();
  }
});

function _showSuggestDropdown(btn) {
  const fullKey = btn.dataset.suggestFor;
  const prefix = btn.dataset.suggestPrefix || '';
  const input = document.querySelector(`.env-value[data-key="${CSS.escape(fullKey)}"]`);
  if (!input || _componentSuggestions.length === 0) return;

  // Build dropdown HTML
  const dd = document.createElement('div');
  dd.className = 'suggest-dropdown';

  // Preselect: component id matching the field prefix
  const preselectedId = prefix
    ? _componentSuggestions.find(c => c.id === prefix)
    : null;
  const suggestions = preselectedId
    ? [preselectedId, ..._componentSuggestions.filter(c => c.id !== prefix)]
    : _componentSuggestions;

  suggestions.forEach(c => {
    const url = c.container_port != null
      ? `http://${c.container_name}:${c.container_port}`
      : null;
    const item = document.createElement('div');
    item.className = 'suggest-item' + (c === preselectedId ? ' preselected' : '');
    item.innerHTML = `<strong>${escHtml(c.id)}</strong>` +
      (url ? `<span class="suggest-url">${escHtml(url)}</span>` : '');
    item.addEventListener('click', () => {
      if (url) {
        input.value = url;
        // Trigger change so the value collector picks it up
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      _closeSuggestDropdown();
    });
    dd.appendChild(item);
  });

  if (suggestions.length === 0) {
    dd.innerHTML = '<div class="suggest-none">No peer components registered.</div>';
  }

  // Position below the button
  const btnRect = btn.getBoundingClientRect();
  dd.style.position = 'fixed';
  dd.style.top = (btnRect.bottom + 4) + 'px';
  dd.style.left = btnRect.left + 'px';

  document.body.appendChild(dd);
  _activeSuggestDropdown = dd;
}

// Bootstrap
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (_activeSuggestDropdown) {
      _closeSuggestDropdown();
    } else if (document.getElementById('vb-modal').classList.contains('open')) {
      closeVolumeBrowser();
    } else if (document.getElementById('onboard-modal').classList.contains('open')) {
      closeOnboardModal();
    } else if (document.getElementById('config-modal').style.display === 'flex') {
      closeConfigModal();
    } else if (document.getElementById('env-modal').classList.contains('open')) {
      closeEnvModal();
    } else {
      closeLogs();
    }
  }
});
// ── Claude auth panel ────────────────────────────────────────────

let claudeLoginId = '';

async function fetchClaudeAuthStatus() {
  try {
    const resp = await fetch('/claude-auth/status', {
      headers: authHeaders(),
      credentials: 'same-origin',
    });
    if (!resp.ok) return;
    renderClaudeAuthStatus(await resp.json());
  } catch (_e) {
    // silently skip — panel is optional
  }
}

function renderClaudeAuthStatus(data) {
  const panel = document.getElementById('claude-auth-panel');
  if (!panel) return;
  panel.style.display = '';

  const badge = document.getElementById('claude-auth-status-badge');
  const detail = document.getElementById('claude-auth-detail');
  const loginSection = document.getElementById('claude-auth-login-section');
  const loginBtn = document.getElementById('claude-auth-login-btn');

  const statusMap = {
    'authenticated':     { cls: 'badge-running',  text: '✓ Authenticated' },
    'not-authenticated': { cls: 'badge-stopped',  text: '✗ Not authenticated' },
    'expiring':          { cls: 'badge-restarting', text: '⚠ Expiring soon' },
    'error':             { cls: 'badge-failed',    text: '⚠ Error' },
  };
  const info = statusMap[data.status] || { cls: 'badge-unknown', text: data.status };
  badge.innerHTML = `<span class="badge ${info.cls}">${info.text}</span>`;

  let detailText = data.detail || '';
  if (data.refresh_status === 'ok') {
    detailText += (detailText ? ' — ' : '') + '✓ Auto-refresh active';
  } else if (data.refresh_status === 'failed') {
    detailText += (detailText ? ' — ' : '') + '⚠ Refresh failed';
    if (data.last_refresh_error) {
      detailText += ': ' + data.last_refresh_error;
    }
  }
  detail.textContent = detailText;

  if (data.status === 'authenticated') {
    loginBtn.style.display = '';
    loginSection.style.display = 'none';
  } else {
    loginBtn.style.display = '';
  }
}

async function startClaudeLogin() {
  const btn = document.getElementById('claude-auth-login-btn');
  const toast = document.getElementById('claude-auth-toast');
  const loginSection = document.getElementById('claude-auth-login-section');
  const errorDiv = document.getElementById('claude-auth-login-error');
  const urlLink = document.getElementById('claude-auth-oauth-url');
  const codeInput = document.getElementById('claude-auth-code-input');
  const submitBtn = document.getElementById('claude-auth-submit-btn');
  const cancelBtn = document.getElementById('claude-auth-cancel-btn');

  if (btn) btn.disabled = true;
  errorDiv.style.display = 'none';
  toast.textContent = 'Starting Claude login…';
  toast.style.color = 'var(--grey)';

  try {
    const resp = await fetch('/claude-auth/login', {
      method: 'POST',
      headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
      credentials: 'same-origin',
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    claudeLoginId = data.login_id;
    urlLink.href = data.oauth_url;
    urlLink.textContent = data.oauth_url;
    codeInput.value = '';
    loginSection.style.display = '';
    submitBtn.disabled = false;
    cancelBtn.disabled = false;
    toast.textContent = 'Visit the URL above, authorize, then paste the code below.';
    toast.style.color = 'var(--blue)';
  } catch (e) {
    toast.textContent = `Login start failed: ${e.message}`;
    toast.style.color = 'var(--red)';
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function completeClaudeLogin() {
  const codeInput = document.getElementById('claude-auth-code-input');
  const submitBtn = document.getElementById('claude-auth-submit-btn');
  const cancelBtn = document.getElementById('claude-auth-cancel-btn');
  const errorDiv = document.getElementById('claude-auth-login-error');
  const toast = document.getElementById('claude-auth-toast');
  const loginSection = document.getElementById('claude-auth-login-section');

  const authCode = codeInput.value.trim();
  if (!authCode) {
    errorDiv.textContent = 'Please paste the authorization code.';
    errorDiv.style.display = '';
    return;
  }
  errorDiv.style.display = 'none';
  submitBtn.disabled = true;
  cancelBtn.disabled = true;
  toast.textContent = 'Completing login…';
  toast.style.color = 'var(--grey)';

  try {
    const resp = await fetch('/claude-auth/login/complete', {
      method: 'POST',
      headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify({ login_id: claudeLoginId, auth_code: authCode }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    if (data.status === 'authenticated') {
      toast.textContent = '✓ Login successful!';
      toast.style.color = 'var(--green)';
      loginSection.style.display = 'none';
      claudeLoginId = '';
      await fetchClaudeAuthStatus();
    } else {
      errorDiv.textContent = data.error || 'Login failed.';
      errorDiv.style.display = '';
      toast.textContent = '';
    }
  } catch (e) {
    errorDiv.textContent = `Login failed: ${e.message}`;
    errorDiv.style.display = '';
    toast.textContent = '';
  } finally {
    submitBtn.disabled = false;
    cancelBtn.disabled = false;
  }
}

async function cancelClaudeLogin() {
  const loginSection = document.getElementById('claude-auth-login-section');
  const toast = document.getElementById('claude-auth-toast');
  const submitBtn = document.getElementById('claude-auth-submit-btn');
  const cancelBtn = document.getElementById('claude-auth-cancel-btn');

  submitBtn.disabled = true;
  cancelBtn.disabled = true;

  if (claudeLoginId) {
    try {
      await fetch('/claude-auth/login/cancel', {
        method: 'POST',
        headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
        credentials: 'same-origin',
        body: JSON.stringify({ login_id: claudeLoginId }),
      });
    } catch (_) { /* best-effort */ }
  }
  claudeLoginId = '';
  loginSection.style.display = 'none';
  toast.textContent = '';
}

async function saveClaudeCredentials() {
  const textarea = document.getElementById('claude-auth-paste-textarea');
  const btn = document.getElementById('claude-auth-paste-btn');
  const msg = document.getElementById('claude-auth-paste-msg');

  const json = textarea.value.trim();
  if (!json) {
    msg.textContent = 'Paste credentials JSON first.';
    msg.style.color = 'var(--red)';
    return;
  }
  btn.disabled = true;
  msg.textContent = 'Saving…';
  msg.style.color = 'var(--grey)';

  try {
    const resp = await fetch('/claude-auth/credentials', {
      method: 'POST',
      headers: Object.assign({'Content-Type': 'application/json'}, authHeaders()),
      credentials: 'same-origin',
      body: JSON.stringify({ credentials_json: json }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    if (data.status === 'authenticated') {
      msg.textContent = '✓ Credentials saved.';
      msg.style.color = 'var(--green)';
      textarea.value = '';
      await fetchClaudeAuthStatus();
    } else {
      msg.textContent = data.error || 'Save failed.';
      msg.style.color = 'var(--red)';
    }
  } catch (e) {
    msg.textContent = `Save failed: ${e.message}`;
    msg.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
  }
}

// Wire up event listeners after DOM is ready
function wireClaudeAuthPanel() {
  const loginBtn = document.getElementById('claude-auth-login-btn');
  const submitBtn = document.getElementById('claude-auth-submit-btn');
  const cancelBtn = document.getElementById('claude-auth-cancel-btn');
  const refreshBtn = document.getElementById('claude-auth-refresh-btn');
  const pasteBtn = document.getElementById('claude-auth-paste-btn');

  if (loginBtn) loginBtn.addEventListener('click', startClaudeLogin);
  if (submitBtn) submitBtn.addEventListener('click', completeClaudeLogin);
  if (cancelBtn) cancelBtn.addEventListener('click', cancelClaudeLogin);
  if (refreshBtn) refreshBtn.addEventListener('click', fetchClaudeAuthStatus);
  if (pasteBtn) pasteBtn.addEventListener('click', saveClaudeCredentials);
}

// ── Server self-update ───────────────────────────────────────────

let selfUpdateInitialDigest = '';

async function checkSelfUpdate() {
  try {
    const resp = await fetch('/system/update', { headers: authHeaders(), credentials: 'same-origin' });
    if (!resp.ok) return;
    const data = await resp.json();
    const btn = document.getElementById('self-update-btn');
    if (data.supported && data.update_available && !btn.disabled) {
      selfUpdateInitialDigest = data.running_digest;
      btn.style.display = '';
    }
  } catch (_) { /* transient — retried on the next check */ }
}

async function triggerSelfUpdate() {
  if (!confirm('Update the central-deploy server to the latest image?\n\nThe dashboard will go down for a few seconds while the server container is recreated.')) return;
  const btn = document.getElementById('self-update-btn');
  btn.disabled = true;
  btn.textContent = 'Updating…';
  try {
    const resp = await fetch('/system/update', {
      method: 'POST',
      headers: authHeaders(),
      credentials: 'same-origin',
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || ('HTTP ' + resp.status));
    }
    // The updater pulls the image, then swaps the container; give it a
    // head start before polling for the recreated server.
    setTimeout(() => pollSelfUpdateRecovery(Date.now()), 8000);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '⬆ Update server';
    alert('Self-update failed to start: ' + e.message);
  }
}

async function pollSelfUpdateRecovery(startedAt) {
  const btn = document.getElementById('self-update-btn');
  if (Date.now() - startedAt > 5 * 60 * 1000) {
    btn.disabled = false;
    btn.textContent = '⬆ Update server';
    alert('Self-update did not complete within 5 minutes — check the server logs.');
    return;
  }
  try {
    const resp = await fetch('/system/update', { headers: authHeaders(), credentials: 'same-origin', cache: 'no-store' });
    if (resp.ok) {
      const data = await resp.json();
      // Reload only once the answering server runs a different image than
      // the one we started from (the old container answers until the swap).
      if (data.running_digest && data.running_digest !== selfUpdateInitialDigest) {
        location.reload();
        return;
      }
    }
  } catch (_) { /* server mid-restart */ }
  setTimeout(() => pollSelfUpdateRecovery(startedAt), 3000);
}

(async () => {
  wireClaudeAuthPanel();
  await loadSettings();
  loadDashboard();
  startAutoRefresh();
  checkSelfUpdate();
  checkCaretakerStatus();
  fetchClaudeAuthStatus();
  setInterval(checkSelfUpdate, 5 * 60 * 1000);
})();
