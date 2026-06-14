let currentUser = null;
let logPollInterval = null;
let editingUserId = null;
let totpSecret = null;
let autoRefresh = false;
let pendingConfigTab = null;
let pendingLogShard = null;

// === HELPERS ===

function $(id) { return document.getElementById(id); }

function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escAttr(str) {
  return escHtml(str).replace(/'/g, '&#39;');
}

function toast(msg, type = 'info', duration = 5000) {
  const icons = {
    success: 'check-circle',
    error: 'alert-circle',
    warning: 'alert-triangle',
    info: 'info',
  };
  const c = $('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  const toastType = type === 'warn' ? 'warning' : type;
  t.className = `toast toast--${toastType}`;
  t.innerHTML = `
    <i data-lucide="${icons[toastType] || 'info'}"></i>
    <span class="toast-msg">${escHtml(msg)}</span>
    <button type="button" class="toast-close" aria-label="Закрыть"><i data-lucide="x"></i></button>
  `;
  t.querySelector('.toast-close').addEventListener('click', () => dismissToast(t));
  c.appendChild(t);
  refreshIcons();
  while (c.children.length > 6) dismissToast(c.firstElementChild);
  t._timer = setTimeout(() => dismissToast(t), duration);
}

function dismissToast(el) {
  if (!el?.parentNode) return;
  clearTimeout(el._timer);
  el.classList.add('toast--out');
  setTimeout(() => el.remove(), 220);
}

let pendingNotices = [];

function notice(msg, type = 'info') {
  if (msg) pendingNotices.push({ msg, type });
}

function flushNotices() {
  pendingNotices.forEach(n => toast(n.msg, n.type));
  pendingNotices = [];
}

function renderLoadError(el, msg, retryFn) {
  const retry = retryFn ? `onclick="${retryFn}()"` : 'onclick="location.reload()"';
  el.innerHTML = `
    <div class="card">
      <div class="empty-state">
        <i data-lucide="alert-circle"></i>
        <p>Не удалось загрузить данные</p>
        <button class="btn btn-outline btn-sm" ${retry}>Повторить</button>
      </div>
    </div>
  `;
  toast(msg, 'error');
  refreshIcons();
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Скопировано', 'success');
  } catch {
    toast('Не удалось скопировать', 'error');
  }
}

function pageHeader(icon, title, subtitle) {
  return `
    <div class="page-header">
      <div>
        <h2><i data-lucide="${icon}"></i>${escHtml(title)}</h2>
        ${subtitle ? `<p>${escHtml(subtitle)}</p>` : ''}
      </div>
    </div>
  `;
}

function isOp() {
  return currentUser && (currentUser.role === 'operator' || currentUser.role === 'admin' || currentUser.role === 'owner');
}

function isAdmin() {
  return currentUser && (currentUser.role === 'admin' || currentUser.role === 'owner');
}

function isOwner() {
  return currentUser && currentUser.role === 'owner';
}

function roleLabel(role) {
  const map = { viewer: 'Наблюдатель', operator: 'Оператор', admin: 'Администратор', owner: 'Владелец' };
  return map[role] || role;
}

function fmtBytes(b) {
  if (!b) return '0 Б';
  const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  const i = Math.floor(Math.log(b) / Math.log(1024));
  return (b / Math.pow(1024, i)).toFixed(1) + ' ' + u[i];
}

function fmtUptime(sec) {
  if (!sec) return '0 с';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h} ч ${m} м`;
  if (m > 0) return `${m} м ${s} с`;
  return `${s} с`;
}

function buildField(key, label, type, val, editable, opts) {
  if (type === 'checkbox') {
    return `<label class="check-row"><input type="checkbox" ${val === 'true' ? 'checked' : ''} data-key="${key}" ${editable ? '' : 'disabled'}><span>${escHtml(label)}</span></label>`;
  }
  if (type === 'select') {
    const options = (opts || []).map(o => `<option value="${escAttr(o)}" ${val === o ? 'selected' : ''}>${escHtml(o)}</option>`).join('');
    return `<div class="config-row"><label>${escHtml(label)}</label><select class="select-styled" data-key="${key}" ${editable ? '' : 'disabled'}><option value="">—</option>${options}</select></div>`;
  }
  return `<div class="config-row"><label>${escHtml(label)}</label><input type="${type}" class="input" value="${escHtml(val)}" data-key="${key}" ${editable ? '' : 'readonly'}></div>`;
}

function buildChecklist(checks) {
  return `<ul class="checklist">${checks.map(c => `
    <li>
      <div class="check-icon ${c.ok ? 'ok' : 'fail'}"><i data-lucide="${c.ok ? 'check' : 'x'}"></i></div>
      <div class="check-body">
        <strong>${escHtml(c.label)}${!c.required ? ' <span class="badge warn">необяз.</span>' : ''}</strong>
        <span>${c.ok ? 'Готово' : escHtml(c.hint || 'Требуется настройка')}</span>
      </div>
    </li>
  `).join('')}</ul>`;
}

function buildCodeBlock(text) {
  return `
    <div class="code-block">
      ${escHtml(text)}
      <button type="button" class="copy-btn" data-copy="${escAttr(text)}" title="Копировать"><i data-lucide="copy"></i></button>
    </div>
  `;
}

function buildConnectionCard(conn) {
  const ips = conn.server_ips && conn.server_ips.length ? conn.server_ips.join(', ') : conn.primary_ip;
  const firewallPorts = (conn.firewall_udp_ports || []).join(', ');

  let html = `
    <div class="card">
      <div class="card-header"><i data-lucide="link-2"></i><h3>Подключение к серверу</h3></div>
      <div class="info-grid">
        <div class="info-item"><label>IP сервера</label><div class="val">${escHtml(ips)}</div></div>
        <div class="info-item"><label>Название кластера</label><div class="val">${escHtml(conn.cluster_name)}</div></div>
        <div class="info-item"><label>Порт Master</label><div class="val">${escHtml(conn.master_port)}</div></div>
  `;

  if (conn.shards_enabled) {
    html += `
        <div class="info-item"><label>Порт Caves</label><div class="val">${escHtml(conn.caves_port)}</div></div>
        <div class="info-item"><label>Порт шардов</label><div class="val">${escHtml(conn.master_shard_port)}</div></div>
    `;
  }

  html += `
        <div class="info-item"><label>Bind IP</label><div class="val">${escHtml(conn.bind_ip)}</div></div>
        <div class="info-item"><label>Режим</label><div class="val">${escHtml(conn.mode_label || (conn.offline ? 'Офлайн' : 'Онлайн'))}</div></div>
      </div>
  `;

  if (firewallPorts) {
    html += `
      <p class="text-muted mt-16">
        <i data-lucide="shield" class="icon-inline"></i>
        Откройте UDP-порты в файрволе и на роутере: <strong>${escHtml(firewallPorts)}</strong>
      </p>
    `;
  }

  if (conn.friend_hint) {
    notice(conn.friend_hint, 'info');
  }

  if (conn.klei_duplicate_hint) {
    notice(conn.klei_duplicate_hint, 'warning');
  }

  if (conn.shards_enabled && conn.master_ip) {
    html += `
      <p class="text-muted-sm mt-12">
        Связь шардов (внутри сервера): Caves → Master по
        <strong>${escHtml(conn.caves_master_ip || conn.master_ip)}:${escHtml(conn.master_shard_port)}</strong>
      </p>
    `;
  }

  if (conn.direct_connect_master) {
    html += `
      <p class="section-label">Прямое подключение — поверхность (Master)</p>
      ${buildCodeBlock(conn.direct_connect_master)}
    `;
  }

  if (conn.direct_connect_caves) {
    html += `
      <p class="section-label">Прямое подключение — пещеры (Caves)</p>
      ${buildCodeBlock(conn.direct_connect_caves)}
    `;
  }

  if (conn.steam_search) {
    html += `
      <p class="section-label">Поиск в браузере игры (Klei)</p>
      <div class="info-item mt-12"><label>Название кластера</label><div class="val">${escHtml(conn.steam_search)}</div></div>
      <p class="text-muted-sm mt-8">В списке должна быть <strong>одна</strong> строка с этим именем. Caves в браузере не отображается.</p>
    `;
  }

  html += '</div>';
  return html;
}

function showError(id, msg) {
  toast(msg, 'error');
  hideError(id);
}

function hideError(id) {
  const el = $(id);
  if (el) el.classList.add('hidden');
}

// === INIT ===

document.addEventListener('DOMContentLoaded', () => {
  $('login-form').addEventListener('submit', handleLogin);
  $('logout-btn').addEventListener('click', handleLogout);
  document.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => navigateTo(el.dataset.page));
  });
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-copy]');
    if (btn && btn.dataset.copy) copyText(btn.dataset.copy);
  });
  refreshIcons();
  checkSession();
});

// === AUTH ===

async function checkSession() {
  try {
    const data = await API.get('/api/auth/me');
    currentUser = data;
    showMainScreen();
  } catch {
    showLoginScreen();
  }
}

function showLoginScreen() {
  $('login-screen').classList.remove('hidden');
  $('main-screen').classList.add('hidden');
  if (logPollInterval) { clearInterval(logPollInterval); logPollInterval = null; }
}

function showMainScreen() {
  $('login-screen').classList.add('hidden');
  $('main-screen').classList.remove('hidden');
  $('user-role-badge').textContent = roleLabel(currentUser.role);
  $('nav-users').classList.toggle('hidden', !isOwner());
  $('nav-audit').classList.toggle('hidden', !isAdmin());
  navigateTo('dashboard');
}

async function handleLogin(e) {
  e.preventDefault();
  hideError('login-error');
  const btn = $('login-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Вход...';
  try {
    const data = await API.post('/api/auth/login', {
      username: $('login-username').value,
      password: $('login-password').value,
      totp_code: $('login-totp').value,
    });
    document.cookie = `session_id=${data.session_id}; path=/; SameSite=Lax; max-age=86400`;
    currentUser = await API.get('/api/auth/me');
    showMainScreen();
  } catch (err) {
    if (err.message.includes('2FA')) {
      $('totp-field').classList.remove('hidden');
      refreshIcons();
    }
    showError('login-error', err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="log-in"></i> Войти';
    refreshIcons();
  }
}

async function handleLogout() {
  try { await API.post('/api/auth/logout'); } catch {}
  document.cookie = 'session_id=; path=/; max-age=0';
  currentUser = null;
  showLoginScreen();
}

// === NAVIGATION ===

function openConfigTab(tab) {
  pendingConfigTab = tab;
  navigateTo('config');
}

function navigateTo(page) {
  if (page !== 'server') stopServerPolling();
  if (page !== 'players') stopPlayersPolling();
  if (page !== 'dashboard') stopDashboardPolling();
  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const pageEl = $(`page-${page}`);
  if (pageEl) {
    pageEl.classList.remove('hidden');
    document.querySelector(`.nav-item[data-page="${page}"]`)?.classList.add('active');
  }
  switch (page) {
    case 'dashboard': renderDashboard(); break;
    case 'setup': renderSetup(); break;
    case 'server': renderServer(); break;
    case 'config': renderConfig(); break;
    case 'mods': renderMods(); break;
    case 'players': renderPlayers(); break;
    case 'logs': renderLogs(); break;
    case 'backups': renderBackups(); break;
    case 'users': renderUsers(); break;
    case 'audit': renderAudit(); break;
    case 'settings': renderSettings(); break;
  }
}

// === DASHBOARD ===

async function renderDashboard() {
  stopDashboardPolling();
  const el = $('page-dashboard');
  el.innerHTML = '<div class="loading-center"><span class="spinner"></span></div>';
  refreshIcons();
  await refreshDashboardData(false);
  dashboardPollInterval = setInterval(() => refreshDashboardData(true), DASHBOARD_POLL_MS);
}

async function refreshDashboardData(silent) {
  const el = $('page-dashboard');
  if (!el || el.classList.contains('hidden')) return;
  try {
    const [status, metrics, setup, playersData] = await Promise.all([
      API.get('/api/server/status'),
      API.get('/api/metrics/system').catch(() => ({})),
      API.get('/api/server/setup').catch(() => null),
      API.get('/api/server/players').catch(() => null),
    ]);

    if (!silent) {
      if (setup && !setup.ready) {
        const tokenMissing = setup.checks?.find(c => c.id === 'token' && c.required && !c.ok);
        const tokenHint = tokenMissing
          ? ' Не хватает Cluster Token — добавьте на странице «Запуск».'
          : '';
        notice(`Сервер не готов к запуску.${tokenHint}`, 'warning');
      } else if (setup?.connection && !setup.connection.offline && !setup.connection.has_token) {
        notice('Cluster Token не задан. Добавьте токен или включите «Офлайн-кластер» в Конфиге.', 'warning');
      }
    }

    let connMini = '';
    if (setup && setup.connection) {
      const c = setup.connection;
      connMini = `
        <div class="card">
          <div class="card-header"><i data-lucide="link-2"></i><h3>Подключение</h3></div>
          <div class="info-grid">
            <div class="info-item"><label>IP</label><div class="val">${escHtml(c.primary_ip)}</div></div>
            <div class="info-item"><label>Кластер</label><div class="val">${escHtml(c.cluster_name)}</div></div>
            <div class="info-item"><label>Порт Master</label><div class="val">${escHtml(c.master_port)}</div></div>
            ${c.shards_enabled ? `<div class="info-item"><label>Порт Caves</label><div class="val">${escHtml(c.caves_port)}</div></div>` : ''}
          </div>
        </div>
      `;
    }

    el.innerHTML = `
      ${pageHeader('layout-dashboard', 'Обзор', 'Статус сервера и системные метрики · обновление каждые 20 с')}
      <div class="grid-2">
        <div class="card">
          <div class="card-header"><i data-lucide="sun"></i><h3>Master</h3></div>
          <div class="value"><span class="status-dot ${status.master.running ? 'online' : 'offline'}"></span>${status.master.running ? 'Онлайн' : 'Офлайн'}</div>
          <div class="sub">PID: ${status.master.pid || '—'} · Аптайм: ${fmtUptime(status.master.uptime || 0)}</div>
        </div>
        <div class="card">
          <div class="card-header"><i data-lucide="mountain"></i><h3>Caves</h3></div>
          <div class="value"><span class="status-dot ${status.caves.running ? 'online' : 'offline'}"></span>${status.caves.running ? 'Онлайн' : 'Офлайн'}</div>
          <div class="sub">PID: ${status.caves.pid || '—'} · Аптайм: ${fmtUptime(status.caves.uptime || 0)}</div>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><i data-lucide="gamepad-2"></i><h3>Игроки онлайн</h3></div>
        <div class="value">${status.players_online != null ? status.players_online : '—'}</div>
        <div class="sub">${(status.online_players || []).map(p => escHtml(p.name || p.klei_id)).join(', ') || 'Никого на сервере'}</div>
        <div class="btn-group mt-12">
          <button class="btn btn-outline btn-sm" onclick="navigateTo('players')">
            <i data-lucide="users"></i> Все игроки
          </button>
        </div>
      </div>
      ${playersData?.dashboard ? buildPlayersDashboard(playersData.dashboard) : ''}
      <div class="grid-3">
        <div class="card">
          <div class="card-header"><i data-lucide="cpu"></i><h3>CPU</h3></div>
          <div class="value">${metrics.cpu_percent != null ? metrics.cpu_percent + '%' : '—'}</div>
        </div>
        <div class="card">
          <div class="card-header"><i data-lucide="memory-stick"></i><h3>RAM</h3></div>
          <div class="value">${metrics.ram_percent != null ? metrics.ram_percent + '%' : '—'}</div>
          <div class="sub">${metrics.ram_used != null ? fmtBytes(metrics.ram_used) + ' / ' + fmtBytes(metrics.ram_total) : ''}</div>
        </div>
        <div class="card">
          <div class="card-header"><i data-lucide="hard-drive"></i><h3>Диск</h3></div>
          <div class="value">${metrics.disk_percent != null ? metrics.disk_percent + '%' : '—'}</div>
          <div class="sub">${metrics.disk_used != null ? fmtBytes(metrics.disk_used) + ' / ' + fmtBytes(metrics.disk_total) : ''}</div>
        </div>
      </div>
      ${connMini}
    `;
    refreshIcons();
    if (!silent) flushNotices();
  } catch (err) {
    if (!silent) renderLoadError(el, err.message, 'renderDashboard');
  }
}

function buildSetupSteps(steps) {
  if (!steps?.length) return '';
  const currentIdx = steps.findIndex(s => s.required && !s.ok);
  const activeIdx = currentIdx === -1 ? steps.length - 1 : currentIdx;
  return `
    <div class="setup-progress">
      ${steps.map((s, i) => `
        <div class="setup-step ${s.ok ? 'done' : ''} ${i === activeIdx && !s.ok ? 'active' : ''}">
          <div class="setup-step-icon">
            <i data-lucide="${s.ok ? 'check-circle' : (i === activeIdx ? 'circle-dot' : 'circle')}"></i>
          </div>
          <div class="setup-step-body">
            <strong>${escHtml(s.label)}</strong>
            <span>${escHtml(s.description)}</span>
            ${!s.ok && s.action_label ? `<span class="setup-step-action">${escHtml(s.action_label)}</span>` : ''}
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

function buildShardArchCard(binding, conn) {
  const m = binding?.master || {};
  const cv = binding?.caves || {};
  const cl = binding?.cluster || {};
  const masterBadge = m.synced
    ? '<span class="badge ok">OK</span>'
    : '<span class="badge warn">не синхр.</span>';
  const cavesBadge = cv.synced
    ? '<span class="badge ok">OK</span>'
    : '<span class="badge warn">не синхр.</span>';
  return `
    <div class="card">
      <div class="card-header"><i data-lucide="layers"></i><h3>Архитектура кластера</h3></div>
      <p class="text-muted">
        DST — это <strong>один кластер</strong> из двух процессов. В браузере Klei виден только Master;
        Caves работает в фоне и подключается по <code>master_ip</code>.
      </p>
      <div class="info-grid mt-12">
        <div class="info-item">
          <label>Master (поверхность)</label>
          <div class="val">порт ${escHtml(m.game_port || '10999')} · is_master=true ${masterBadge}</div>
        </div>
        <div class="info-item">
          <label>Caves (пещеры)</label>
          <div class="val">порт ${escHtml(cv.game_port || '11000')} · is_master=false ${cavesBadge}</div>
        </div>
        <div class="info-item">
          <label>Связь шардов</label>
          <div class="val">${escHtml(cl.master_ip || '127.0.0.1')}:${escHtml(cl.master_shard_port || '10888')}</div>
        </div>
        <div class="info-item">
          <label>Caves master_ip</label>
          <div class="val">${escHtml(cv.master_ip || '—')}</div>
        </div>
      </div>
      ${!cv.synced && conn?.shards_enabled ? `
        <p class="text-muted-sm mt-12">
          <i data-lucide="alert-triangle" class="icon-inline"></i>
          Caves не привязан — в браузере может появиться второй сервер с тем же именем.
        </p>
      ` : ''}
    </div>
  `;
}

// === SETUP ===

async function renderSetup() {
  const el = $('page-setup');
  el.innerHTML = '<div class="loading-center"><span class="spinner"></span></div>';
  refreshIcons();
  try {
    const setup = await API.get('/api/server/setup');
    const { ready, launch_ready, checks, connection, launch_steps, binding } = setup;

    if (!launch_ready && !ready) {
      notice('Завершите шаги ниже перед запуском.', 'warning');
    }

    const clusterIniOk = checks.find(c => c.id === 'cluster_ini')?.ok;
    const tokenCheck = checks.find(c => c.id === 'token');
    const shardLinkOk = checks.find(c => c.id === 'shard_link')?.ok;

    el.innerHTML = `
      ${pageHeader('rocket', 'Запуск сервера', 'Пошаговая настройка кластера Master + Caves')}

      <div class="card card-accent">
        <div class="card-header"><i data-lucide="list-ordered"></i><h3>Порядок запуска</h3></div>
        ${buildSetupSteps(launch_steps)}
      </div>

      <div class="grid-2">
        <div class="card card-accent">
          <div class="card-header"><i data-lucide="globe"></i><h3>Онлайн (Klei)</h3></div>
          <p class="text-muted">
            Сервер в браузере игры. Нужен <strong>Cluster Token</strong>.
            Создаёт конфиг с привязкой шардов: Master (10999), Caves (11000), связь (10888).
          </p>
          <div class="grid-2 mt-12">
            <div class="field">
              <label>Название кластера</label>
              <input type="text" class="input" id="online-name-input" value="${escAttr(connection.cluster_name !== 'Игра с друзьями' ? connection.cluster_name : 'Мой DST сервер')}" placeholder="Мой DST сервер" ${isOp() ? '' : 'readonly'}>
            </div>
            <div class="field">
              <label>Пароль сервера</label>
              <input type="password" class="input" id="online-password-input" placeholder="Пароль для игроков" ${isOp() ? '' : 'readonly'}>
            </div>
          </div>
          <div class="btn-group mt-12">
            <button class="btn btn-primary" onclick="applyOnlinePreset()" ${isOp() ? '' : 'disabled'}>
              <i data-lucide="zap"></i> Настроить онлайн-сервер
            </button>
          </div>
          <p class="text-muted-sm mt-12">
            После пресета сохраните токен Klei. В браузере — <strong>одна</strong> строка с названием кластера.
          </p>
        </div>

        <div class="card">
          <div class="card-header"><i data-lucide="users"></i><h3>Офлайн (по IP)</h3></div>
          <p class="text-muted">
            Без токена Klei. Подключение через <code>c_connect</code> на порт Master.
            Caves всё равно нужен для пещер (запускается вторым).
          </p>
          <div class="grid-2 mt-12">
            <div class="field">
              <label>Название сервера</label>
              <input type="text" class="input" id="friends-name-input" value="Игра с друзьями" placeholder="Игра с друзьями" ${isOp() ? '' : 'readonly'}>
            </div>
            <div class="field">
              <label>Пароль (необязательно)</label>
              <input type="password" class="input" id="friends-password-input" placeholder="Пароль для друга" ${isOp() ? '' : 'readonly'}>
            </div>
          </div>
          <div class="btn-group mt-12">
            <button class="btn btn-outline" onclick="applyFriendsPreset()" ${isOp() ? '' : 'disabled'}>
              <i data-lucide="zap"></i> Настроить офлайн
            </button>
          </div>
        </div>
      </div>

      ${buildShardArchCard(binding, connection)}

      <div class="grid-2">
        <div class="card">
          <div class="card-header"><i data-lucide="list-checks"></i><h3>Чеклист готовности</h3></div>
          ${buildChecklist(checks)}
        </div>

        <div class="card">
          <div class="card-header"><i data-lucide="wrench"></i><h3>Токен и запуск</h3></div>

          ${clusterIniOk ? `
            <p class="text-muted">
              <i data-lucide="check" class="icon-inline icon-ok"></i>
              Конфиг создан: cluster.ini, Master, Caves (с master_ip).
              <a href="#" onclick="navigateTo('config');return false;">Конфиг</a>
            </p>
          ` : `
            <p class="text-muted">Сначала выберите пресет <strong>Онлайн</strong> или <strong>Офлайн</strong>.</p>
          `}

          ${clusterIniOk && !shardLinkOk ? `
            <p class="text-muted-sm mt-12">
              <i data-lucide="alert-triangle" class="icon-inline"></i>
              Привязка шардов неполная — возможен дубль в браузере Klei.
            </p>
            <div class="btn-group mt-12">
              <button class="btn btn-warning btn-sm" onclick="repairShardLink()" ${isOp() ? '' : 'disabled'}>
                <i data-lucide="wrench"></i> Проверить привязку
              </button>
              <button class="btn btn-outline btn-sm" onclick="openConfigTab('caves')">
                <i data-lucide="settings"></i> Конфиг → Caves
              </button>
            </div>
          ` : clusterIniOk ? `
            <div class="btn-group mt-12">
              <button class="btn btn-outline btn-sm" onclick="repairShardLink()" ${isOp() ? '' : 'disabled'}>
                <i data-lucide="refresh-cw"></i> Проверить привязку
              </button>
            </div>
          ` : ''}

          ${tokenCheck && !tokenCheck.ok && !connection.offline ? `
            <div class="divider-top">
              <p class="text-muted">
                Для онлайн-сервера получите токен на
                <a href="https://accounts.klei.com/account/game/servers" target="_blank" rel="noopener">accounts.klei.com</a>
              </p>
              <div class="field">
                <label>Cluster Token</label>
                <input type="password" class="input" id="setup-token-input" placeholder="Вставьте токен Klei" ${isAdmin() ? '' : 'readonly'}>
              </div>
              <div class="btn-group">
                <button class="btn btn-primary btn-sm" onclick="saveSetupToken()" ${isAdmin() ? '' : 'disabled'}>
                  <i data-lucide="key"></i> Сохранить токен
                </button>
                <button class="btn btn-outline btn-sm" onclick="openConfigTab('token')">
                  <i data-lucide="settings"></i> Конфиг → Токен
                </button>
              </div>
            </div>
          ` : connection.offline ? `
            <p class="text-muted-sm mt-12">
              <i data-lucide="info" class="icon-inline"></i>
              Офлайн-режим: токен Klei не нужен. Подключение по IP на порт Master.
            </p>
          ` : tokenCheck?.ok ? `
            <p class="text-muted-sm mt-12"><i data-lucide="check" class="icon-inline icon-ok"></i> Cluster Token сохранён</p>
          ` : ''}

          <div class="btn-group mt-16">
            <button class="btn btn-success btn-sm" onclick="navigateTo('server')" ${ready ? '' : 'disabled'}>
              <i data-lucide="play"></i> Перейти к запуску шардов
            </button>
          </div>
        </div>
      </div>

      ${buildConnectionCard(connection)}

      <div class="card">
        <div class="card-header"><i data-lucide="book-open"></i><h3>Инструкция</h3></div>
        <ol class="text-muted setup-steps">
          <li><strong>Установка:</strong> <a href="#" onclick="navigateTo('server');return false;">Сервер</a> → «Установить DST» (или <code>install.sh</code> на хосте)</li>
          <li><strong>Пресет:</strong> «Онлайн» (браузер Klei + токен) или «Офлайн» (подключение по IP)</li>
          <li><strong>Токен:</strong> только для онлайн — сохраните Cluster Token на этой странице</li>
          <li><strong>Порты:</strong> откройте UDP ${escHtml((connection.firewall_udp_ports || []).join(', ') || '10999, 11000, 10888')} на файрволе и роутере</li>
          <li><strong>Запуск:</strong> <a href="#" onclick="navigateTo('server');return false;">Сервер</a> → Старт <strong>Master</strong>, затем Старт <strong>Caves</strong> (порядок важен)</li>
          <li><strong>Дубль в браузере:</strong> если два одинаковых сервера — Стоп оба шарда → «Проверить привязку» → запустить снова</li>
          <li><strong>Игроки:</strong> онлайн — поиск по названию кластера; офлайн — <code>c_connect</code> из карточки выше</li>
          <li><strong>Админы:</strong> Klei ID (<code>KU_...</code>) в <a href="#" onclick="openConfigTab('adminlist');return false;">adminlist.txt</a></li>
        </ol>
      </div>
    `;
    refreshIcons();
    flushNotices();
  } catch (err) {
    renderLoadError(el, err.message, 'renderSetup');
  }
}

async function initCluster() {
  return applyOnlinePreset();
}

async function applyPreset(mode) {
  const isFriends = mode === 'friends';
  const name = $(isFriends ? 'friends-name-input' : 'online-name-input')?.value?.trim()
    || (isFriends ? 'Игра с друзьями' : 'Мой DST сервер');
  const password = $(isFriends ? 'friends-password-input' : 'online-password-input')?.value ?? '';
  const label = isFriends ? 'офлайн' : 'онлайн';
  if (!confirm(`Применить ${label}-пресет? Будут перезаписаны cluster.ini, Master/server.ini и Caves/server.ini (с привязкой master_ip).`)) return;
  try {
    toast(`Настройка ${label}-сервера...`, 'info');
    const data = await API.post(`/api/server/preset/${mode}`, { cluster_name: name, password });
    if (data.success) {
      const a = data.applied || {};
      toast(
        `Готово (${label})! Master :${a.master_port || 10999}, Caves :${a.caves_port || 11000}, связь :${a.shard_port || 10888}`,
        'success'
      );
      renderSetup();
    } else {
      toast('Ошибка: ' + (data.error || 'неизвестная'), 'error');
    }
  } catch (err) { toast(err.message, 'error'); }
}

async function repairShardLink() {
  try {
    toast('Проверка привязки шардов...', 'info');
    const data = await API.post('/api/server/setup/repair-shards');
    toast(data.message || 'Готово', data.changed ? 'success' : 'info');
    renderSetup();
  } catch (err) { toast(err.message, 'error'); }
}

async function applyOnlinePreset() {
  return applyPreset('online');
}

async function applyFriendsPreset() {
  return applyPreset('friends');
}

async function saveSetupToken() {
  const token = $('setup-token-input')?.value;
  if (!token) { toast('Введите токен', 'error'); return; }
  try {
    await API.put('/api/config/token', { token });
    toast('Токен сохранён', 'success');
    if ($('setup-token-input')) $('setup-token-input').value = '';
    renderSetup();
  } catch (err) { toast(err.message, 'error'); }
}

let serverStatusInterval = null;
let regenPollInterval = null;
let playersPollInterval = null;
let dashboardPollInterval = null;
const DASHBOARD_POLL_MS = 20000;
const PLAYERS_POLL_MS = 12000;

function stopServerPolling() {
  if (serverStatusInterval) {
    clearInterval(serverStatusInterval);
    serverStatusInterval = null;
  }
  stopRegenPolling();
}

function stopRegenPolling() {
  if (regenPollInterval) {
    clearInterval(regenPollInterval);
    regenPollInterval = null;
  }
}

function stopPlayersPolling() {
  if (playersPollInterval) {
    clearInterval(playersPollInterval);
    playersPollInterval = null;
  }
}

function stopDashboardPolling() {
  if (dashboardPollInterval) {
    clearInterval(dashboardPollInterval);
    dashboardPollInterval = null;
  }
}

function renderShardStatusHtml(s) {
  const sourceLabels = {
    process: 'процесс',
    proc: 'процесс',
    pgrep: 'процесс',
    ps: 'процесс',
    psutil: 'процесс',
    infer: 'процесс',
    port: 'порт UDP',
    log: 'лог',
    registry: 'панель',
    discovered: 'обнаружен',
    tracked: 'панель',
    systemd: 'systemd',
  };
  const confirmed = s.confirmed !== false;
  const dotClass = s.running ? (confirmed ? 'online' : 'warning') : 'offline';
  const statusLabel = s.running
    ? (confirmed ? 'Онлайн' : 'Онлайн (не подтверждён)')
    : 'Офлайн';
  const src = s.systemd
    ? 'systemd'
    : (sourceLabels[s.source] || (s.external ? 'внешний' : ''));
  const srcLabel = src ? ` · ${src}` : '';
  const portHint = s.port_open && !s.pid
    ? `<p class="text-muted-sm mt-8"><i data-lucide="info" class="icon-inline"></i> Порт занят, PID недоступен (права /proc)</p>`
    : '';
  const uncertainHint = s.running && !confirmed
    ? `<p class="text-muted-sm mt-8"><i data-lucide="alert-triangle" class="icon-inline"></i> Процесс не найден — нажмите «Рестарт» для управления через панель</p>`
    : '';
  const dup = (s.pids && s.pids.length > 1)
    ? `<p class="text-muted-sm mt-8"><i data-lucide="alert-triangle" class="icon-inline"></i> Дубль процессов: ${escHtml(s.pids.join(', '))}</p>`
    : '';
  return `
    <div class="shard-status">
      <div class="shard-status-row">
        <span class="status-dot ${dotClass}"></span>
        <span>${statusLabel}</span>
      </div>
      <div class="shard-status-meta">PID: ${s.pid || '—'} · ${fmtUptime(s.uptime || 0)}${srcLabel}</div>
      ${portHint}
      ${uncertainHint}
      ${dup}
    </div>
  `;
}

function renderPrereqHtml(prereq, status) {
  if (status?.running && status?.confirmed !== false) {
    return `<p class="text-muted-sm mt-12"><i data-lucide="check" class="icon-inline icon-ok"></i> Шард работает</p>`;
  }
  if (status?.running && status?.confirmed === false) {
    return `<p class="text-muted-sm mt-12"><i data-lucide="alert-triangle" class="icon-inline"></i> Статус не подтверждён — используйте «Рестарт»</p>`;
  }
  if (!prereq?.checks) return '';
  const failed = prereq.checks.filter(c => c.required && !c.ok);
  if (!failed.length) {
    return `<p class="text-muted-sm mt-12"><i data-lucide="check" class="icon-inline icon-ok"></i> Готов к запуску</p>`;
  }
  return `<ul class="checklist checklist-compact mt-12">${failed.map(c => `
    <li>
      <div class="check-icon fail"><i data-lucide="x"></i></div>
      <div class="check-body"><strong>${escHtml(c.label)}</strong><span>${escHtml(c.hint)}</span></div>
    </li>
  `).join('')}</ul>`;
}

function openLogsTab(shard) {
  pendingLogShard = shard || 'Master';
  navigateTo('logs');
}

function showLogTailModal(shard, lines, logPath) {
  const body = (lines && lines.length)
    ? `<pre class="log-tail-preview">${escHtml(lines.join('\n'))}</pre>`
    : `<p class="text-muted">Строки лога не найдены.</p>`;
  const pathHint = logPath
    ? `<p class="text-muted-sm mt-12">Файл: <code class="code-inline">${escHtml(logPath)}</code></p>`
    : '';
  showModal(`Лог ${shard}`, `
    ${body}
    ${pathHint}
    <div class="btn-group">
      <button class="btn btn-outline" onclick="openLogsTab('${escAttr(shard)}');closeModal();">Открыть вкладку «Логи»</button>
      <button class="btn btn-primary" onclick="closeModal()">Закрыть</button>
    </div>
  `);
}

async function showShardLogsOnError(shard, initialLines) {
  if (initialLines?.length) {
    showLogTailModal(shard, initialLines);
    return;
  }
  try {
    const data = await API.get(`/api/server/logs/${shard}?lines=120`);
    const lines = data.lines || [];
    showLogTailModal(shard, lines.length ? lines : [data.message || 'Лог пуст или ещё не создан'], data.path);
  } catch (err) {
    showLogTailModal(shard, [err.message || 'Не удалось загрузить лог']);
  }
}

async function waitForShardRunning(shard, seconds = 10) {
  for (let i = 0; i < seconds; i++) {
    await new Promise(r => setTimeout(r, 1000));
    const status = await API.get('/api/server/status');
    applyServerStatus(status);
    const s = shard === 'Master' ? status.master : status.caves;
    if (s?.running) return true;
  }
  return false;
}

function buildServerStatusHtml(health, warnings) {
  const pills = [];
  if (health) {
    if (health.caves_linked) {
      pills.push('<span class="status-pill status-pill--ok"><i data-lucide="check-circle"></i> Caves подключён к Master</span>');
    } else if (health.needs_master_restart) {
      pills.push('<span class="status-pill status-pill--warn"><i data-lucide="alert-triangle"></i> Перезапустите кластер: shard_enabled был выключен</span>');
    } else if (health.master_running && !health.caves_running) {
      pills.push('<span class="status-pill status-pill--err"><i data-lucide="alert-circle"></i> Caves не запущен — порталы отключены</span>');
    } else if (health.master_running && health.caves_running && !health.caves_linked) {
      pills.push('<span class="status-pill status-pill--warn"><i data-lucide="alert-triangle"></i> Caves не связан — перезапустите кластер</span>');
    }
  }
  (warnings || []).forEach(w => {
    pills.push(`<span class="status-pill status-pill--warn"><i data-lucide="alert-triangle"></i> ${escHtml(w.message)}</span>`);
  });
  return pills.length ? `<div class="status-bar">${pills.join('')}</div>` : '';
}

let lastServerHealthKey = '';

function serverHealthKey(health, warnings) {
  const parts = [];
  if (health) {
    parts.push(
      health.caves_linked ? 'linked' : 'unlinked',
      health.caves_running ? 'caves-up' : 'caves-down',
      health.master_running ? 'master-up' : 'master-down',
      health.needs_master_restart ? 'restart' : ''
    );
  }
  (warnings || []).forEach(w => parts.push(w.message || ''));
  return parts.join('|');
}

function notifyServerHealth(health, warnings) {
  if (!health) return;
  if (health.caves_linked) return;
  if (health.needs_master_restart) {
      notice('Перезапустите кластер: Master стартовал с shard_enabled=false.', 'warning');
    return;
  }
  if (health.master_running && !health.caves_running) {
    notice('Caves не запущен — пещеры и порталы отключены. Нажмите «Запустить кластер».', 'error');
    return;
  }
  if (health.master_running && health.caves_running && !health.caves_linked) {
    notice('Caves ещё не связан с Master — нажмите «Перезапустить кластер».', 'warning');
  }
}

function applyServerStatus(data) {
  if ($('server-warnings')) {
    const warnings = data.warnings || [];
    const health = data.shard_health;
    const filteredWarnings = (warnings || []).filter(w => {
      if (!health?.caves_linked) return true;
      const msg = (w.message || '').toLowerCase();
      return !(
        msg.includes('без режима шардов')
        || msg.includes('shard server mode disabled')
      );
    });
    const healthKey = serverHealthKey(health, filteredWarnings);
    const onServerPage = $('page-server') && !$('page-server').classList.contains('hidden');
    if (onServerPage && healthKey !== lastServerHealthKey) {
      lastServerHealthKey = healthKey;
      notifyServerHealth(health, filteredWarnings);
      flushNotices();
    }
    $('server-warnings').innerHTML = buildServerStatusHtml(health, filteredWarnings);
  }
  if ($('master-status') && data.master) {
    $('master-status').innerHTML = renderShardStatusHtml(data.master);
  }
  if ($('caves-status') && data.caves) {
    $('caves-status').innerHTML = renderShardStatusHtml(data.caves);
  }
  if ($('master-readiness') && data.master?.prerequisites) {
    $('master-readiness').innerHTML = renderPrereqHtml(data.master.prerequisites, data.master);
  }
  if ($('caves-readiness') && data.caves?.prerequisites) {
    $('caves-readiness').innerHTML = renderPrereqHtml(data.caves.prerequisites, data.caves);
  }
  refreshIcons();
}

// === SERVER ===

async function renderServer() {
  stopServerPolling();
  lastServerHealthKey = '';
  const el = $('page-server');
  el.innerHTML = `
    ${pageHeader('server', 'Управление сервером', 'Запуск, остановка и проверка шардов DST')}
    <div class="card card-accent" id="worlds-panel">
      <div class="card-header"><i data-lucide="globe"></i><h3>Миры</h3></div>
      <p class="text-muted">Выберите, какой мир запускать при старте кластера. Можно хранить несколько именованных сейвов.</p>
      <div id="worlds-active-block"><span class="spinner"></span></div>
      <div class="field mt-12">
        <label>Режим запуска</label>
        <select id="world-mode-select" class="select-styled" onchange="onWorldModeChange()" ${isOp() ? '' : 'disabled'}>
          <option value="current">Текущий сейв на диске</option>
          <option value="library">Мир из библиотеки</option>
          <option value="new">Новый мир (очистить сейвы)</option>
        </select>
      </div>
      <div class="field mt-12 hidden" id="world-library-select-wrap">
        <label>Мир из библиотеки</label>
        <select id="world-library-select" class="select-styled" ${isOp() ? '' : 'disabled'}></select>
      </div>
      <div id="world-readiness-block" class="mt-12"></div>
      <div class="btn-group mt-12">
        <button class="btn btn-primary btn-sm" onclick="applyWorldSelection()" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="check"></i> Применить выбор
        </button>
        <button class="btn btn-outline btn-sm" onclick="showCreateWorldModal()" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="plus"></i> Добавить мир
        </button>
        <button class="btn btn-outline btn-sm" onclick="importCurrentWorldPrompt()" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="import"></i> Импорт текущего сейва
        </button>
      </div>
      <div id="worlds-list-block" class="mt-16"></div>
    </div>
    <div class="card card-accent">
      <div class="card-header"><i data-lucide="layers"></i><h3>Кластер Master + Caves</h3></div>
      <p class="text-muted">Остановка: Caves → Master. Запуск: Master, затем Caves (автоматически). Перед первым запуском настройте конфиг.</p>
      <div class="btn-group">
        <button class="btn btn-success btn-sm" onclick="clusterAction('start')" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="play"></i> Запустить кластер
        </button>
        <button class="btn btn-danger btn-sm" onclick="clusterAction('stop')" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="square"></i> Остановить кластер
        </button>
        <button class="btn btn-warning btn-sm" onclick="clusterAction('restart')" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="rotate-cw"></i> Перезапустить кластер
        </button>
        <button class="btn btn-outline btn-sm" onclick="navigateTo('config')" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="settings-2"></i> Конфиг
        </button>
      </div>
    </div>
    <div class="card card-accent">
      <div class="card-header"><i data-lucide="map"></i><h3>Пересборка мира</h3></div>
      <p class="text-muted">
        Останавливает кластер, архивирует сейвы (<code>session</code>, <code>Save</code>, <code>backup</code>)
        в <code>*.regen.&lt;timestamp&gt;</code> и запускает генерацию нового мира (Master + Caves).
        Текущий мир будет потерян — сделайте бэкап заранее.
      </p>
      <div class="btn-group">
        <button class="btn btn-warning btn-sm" id="regen-world-btn" onclick="regenerateWorld()" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="map-pin-off"></i> Пересобрать мир
        </button>
      </div>
      <div class="progress-wrap" id="regen-progress-wrap">
        <div class="progress-label">
          <span id="regen-progress-text">Подготовка...</span>
          <span id="regen-progress-pct">0%</span>
        </div>
        <div class="progress-bar">
          <div class="progress-bar-fill" id="regen-progress-fill"></div>
        </div>
      </div>
    </div>
    <div id="server-warnings"></div>
    <div class="grid-2">
      <div class="card shard-card">
        <div class="card-header"><i data-lucide="sun"></i><h3>Master</h3></div>
        <div id="master-status"><span class="spinner"></span></div>
        <div id="master-readiness"></div>
      </div>
      <div class="card shard-card">
        <div class="card-header"><i data-lucide="mountain"></i><h3>Caves</h3></div>
        <div id="caves-status"><span class="spinner"></span></div>
        <div id="caves-readiness"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><i data-lucide="download"></i><h3>Установка и обновление</h3></div>
      <p class="text-muted">Скачивание и обновление файлов DST через SteamCMD. Может занять несколько минут.</p>
      <div class="btn-group">
        <button class="btn btn-primary btn-sm" onclick="installServer()" ${isOwner() ? '' : 'disabled'}>
          <i data-lucide="package-down"></i> Установить DST
        </button>
        <button class="btn btn-warning btn-sm" onclick="updateServer()" ${isOwner() ? '' : 'disabled'}>
          <i data-lucide="refresh-cw"></i> Принудительное обновление
        </button>
      </div>
    </div>
  `;
  refreshIcons();
  await loadWorldsPanel();
  await updateServerStatus();
  await checkRegenWorldStatus();
  serverStatusInterval = setInterval(updateServerStatus, 5000);
}

let worldsCache = null;

function worldModeLabel(mode) {
  return { current: 'Текущий на диске', library: 'Из библиотеки', new: 'Новый мир' }[mode] || mode;
}

function onWorldModeChange() {
  const mode = $('world-mode-select')?.value;
  const wrap = $('world-library-select-wrap');
  if (wrap) wrap.classList.toggle('hidden', mode !== 'library');
  refreshWorldReadiness();
}

async function refreshWorldReadiness() {
  const block = $('world-readiness-block');
  if (!block) return;
  try {
    const mode = $('world-mode-select')?.value || 'current';
    const worldId = $('world-library-select')?.value || '';
    const url = worldId && mode === 'library'
      ? `/api/worlds/readiness?world_id=${encodeURIComponent(worldId)}`
      : '/api/worlds/readiness';
    const data = await API.get(url);
    const checks = (data.checks || []).map(c => `
      <div class="setup-step ${c.ok ? 'done' : (c.required ? '' : '')}">
        <div class="setup-step-icon"><i data-lucide="${c.ok ? 'check-circle' : 'alert-circle'}"></i></div>
        <div class="setup-step-body">
          <strong>${escHtml(c.label)}</strong>
          <span>${escHtml(c.hint || '')}</span>
        </div>
      </div>
    `).join('');
    block.innerHTML = `
      <div class="setup-progress">${checks}</div>
      ${data.ready ? '<p class="text-muted-sm mt-8"><i data-lucide="check" class="icon-inline"></i> Готов к запуску</p>' : ''}
    `;
    refreshIcons();
  } catch {
    block.innerHTML = '';
  }
}

async function loadWorldsPanel() {
  try {
    const data = await API.get('/api/worlds');
    worldsCache = data;
    const active = data.active || { mode: 'current', world_id: null };
    const modeSel = $('world-mode-select');
    const libSel = $('world-library-select');
    if (modeSel) modeSel.value = active.mode || 'current';
    onWorldModeChange();

    if (libSel) {
      const worlds = data.worlds || [];
      libSel.innerHTML = worlds.length
        ? worlds.map(w => `<option value="${escAttr(w.id)}" ${w.id === active.world_id ? 'selected' : ''}>${escHtml(w.name)} (${fmtBytes(w.total_bytes || 0)})</option>`).join('')
        : '<option value="">— нет сохранённых миров —</option>';
      if (active.world_id) libSel.value = active.world_id;
    }

    const activeBlock = $('worlds-active-block');
    if (activeBlock) {
      const name = active.mode === 'library'
        ? (data.worlds || []).find(w => w.id === active.world_id)?.name || 'не выбран'
        : worldModeLabel(active.mode);
      const disk = data.current_on_disk || {};
      activeBlock.innerHTML = `
        <p><strong>Активный выбор:</strong> ${escHtml(name)}</p>
        <p class="text-muted-sm">На диске: Master ${disk.master?.has_data ? fmtBytes(disk.master.size_bytes) : '—'}, Caves ${disk.caves?.has_data ? fmtBytes(disk.caves.size_bytes) : '—'}</p>
      `;
    }

    const listBlock = $('worlds-list-block');
    if (listBlock) {
      const worlds = data.worlds || [];
      if (!worlds.length) {
        listBlock.innerHTML = '<p class="text-muted-sm">Библиотека пуста. Импортируйте текущий сейв или создайте пустой слот.</p>';
      } else {
        listBlock.innerHTML = `
          <div class="table-wrap">
            <table>
              <tr><th>Название</th><th>Master</th><th>Caves</th><th>Размер</th><th>Обновлён</th><th></th></tr>
              ${worlds.map(w => `
                <tr>
                  <td>${escHtml(w.name)}</td>
                  <td>${w.master?.has_data ? '<span class="badge ok">да</span>' : '<span class="badge warn">—</span>'}</td>
                  <td>${w.caves?.has_data ? '<span class="badge ok">да</span>' : '<span class="badge warn">—</span>'}</td>
                  <td>${fmtBytes(w.total_bytes || 0)}</td>
                  <td>${escHtml((w.updated_at || w.created_at || '').slice(0, 19))}</td>
                  <td class="table-actions">
                    <button class="btn btn-outline btn-sm" onclick="selectLibraryWorld('${escAttr(w.id)}')" title="Выбрать" ${isOp() ? '' : 'disabled'}><i data-lucide="play"></i></button>
                    <button class="btn btn-outline btn-sm" onclick="captureWorldToLibrary('${escAttr(w.id)}')" title="Сохранить текущий сейв сюда" ${isOp() ? '' : 'disabled'}><i data-lucide="save"></i></button>
                    <button class="btn btn-outline btn-sm" onclick="renameWorldPrompt('${escAttr(w.id)}', '${escAttr(w.name)}')" title="Переименовать" ${isOp() ? '' : 'disabled'}><i data-lucide="pencil"></i></button>
                    <button class="btn btn-danger btn-sm" onclick="deleteWorldFromLibrary('${escAttr(w.id)}', '${escAttr(w.name)}')" title="Удалить" ${isOp() ? '' : 'disabled'}><i data-lucide="trash-2"></i></button>
                  </td>
                </tr>
              `).join('')}
            </table>
          </div>
        `;
      }
    }
    await refreshWorldReadiness();
    refreshIcons();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function applyWorldSelection() {
  const mode = $('world-mode-select')?.value || 'current';
  const worldId = $('world-library-select')?.value || null;
  if (mode === 'library' && !worldId) {
    toast('Выберите мир из списка', 'error');
    return;
  }
  try {
    const data = await API.post('/api/worlds/active', { mode, world_id: worldId });
    if (data.success) {
      toast('Выбор мира сохранён', 'success');
      await loadWorldsPanel();
    } else toast(data.error || 'Ошибка', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function selectLibraryWorld(worldId) {
  if ($('world-mode-select')) $('world-mode-select').value = 'library';
  onWorldModeChange();
  if ($('world-library-select')) $('world-library-select').value = worldId;
  await applyWorldSelection();
}

function showCreateWorldModal() {
  const name = prompt('Название нового мира:', 'Мой мир');
  if (!name) return;
  const fromCurrent = confirm('Сохранить в библиотеку текущий сейв с диска?\n\nOK — скопировать текущий сейв\nОтмена — пустой слот (для нового мира)');
  createWorldEntry(name.trim(), fromCurrent);
}

async function createWorldEntry(name, fromCurrent) {
  try {
    const data = await API.post('/api/worlds', { name, from_current: fromCurrent, activate: true });
    if (data.success) {
      toast(data.message || 'Мир создан', 'success');
      await loadWorldsPanel();
    } else toast(data.error || 'Ошибка', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

function importCurrentWorldPrompt() {
  const name = prompt('Имя для импорта текущего сейва:', 'Импорт ' + new Date().toLocaleDateString());
  if (!name) return;
  importCurrentWorldEntry(name.trim());
}

async function importCurrentWorldEntry(name) {
  try {
    const data = await API.post('/api/worlds/import-current', { name, activate: true });
    if (data.success) {
      toast(data.message || 'Сейв импортирован', 'success');
      await loadWorldsPanel();
    } else toast(data.error || 'Ошибка', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function captureWorldToLibrary(worldId) {
  if (!confirm('Перезаписать этот слот текущим сейвом с диска? Кластер должен быть остановлен.')) return;
  try {
    const data = await API.post(`/api/worlds/${encodeURIComponent(worldId)}/capture`);
    if (data.success) {
      toast(data.message || 'Сохранено', 'success');
      await loadWorldsPanel();
    } else toast(data.error || 'Ошибка', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function renameWorldPrompt(worldId, currentName) {
  const name = prompt('Новое название:', currentName);
  if (!name || name.trim() === currentName) return;
  try {
    const data = await API.put(`/api/worlds/${encodeURIComponent(worldId)}`, { name: name.trim() });
    if (data.success) {
      toast('Название обновлено', 'success');
      await loadWorldsPanel();
    } else toast(data.error || 'Ошибка', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function deleteWorldFromLibrary(worldId, name) {
  if (!confirm(`Удалить мир «${name}» из библиотеки?`)) return;
  try {
    const data = await API.del(`/api/worlds/${encodeURIComponent(worldId)}`);
    if (data.success) {
      toast('Мир удалён', 'success');
      await loadWorldsPanel();
    } else toast(data.error || 'Ошибка', 'error');
  } catch (err) { toast(err.message, 'error'); }
}

function applyRegenProgress(status) {
  const wrap = $('regen-progress-wrap');
  const fill = $('regen-progress-fill');
  const text = $('regen-progress-text');
  const pct = $('regen-progress-pct');
  const btn = $('regen-world-btn');
  if (!wrap || !fill || !text || !pct) return;

  if (!status || (!status.active && status.step === 'idle')) {
    wrap.classList.remove('visible');
    if (btn) btn.disabled = !isOp();
    return;
  }

  wrap.classList.add('visible');
  const percent = status.percent || 0;
  fill.style.width = `${percent}%`;
  fill.classList.toggle('error', status.step === 'error');
  fill.classList.toggle('done', status.step === 'done' && !status.error);
  text.textContent = status.message || status.step || 'Выполняется...';
  pct.textContent = `${percent}%`;
  if (btn) btn.disabled = Boolean(status.active);
}

async function checkRegenWorldStatus() {
  try {
    const status = await API.get('/api/server/regenerate-world/status');
    applyRegenProgress(status);
    if (status.active) {
      if (!regenPollInterval) {
        regenPollInterval = setInterval(pollRegenWorldStatus, 2000);
      }
    } else if (regenPollInterval) {
      stopRegenPolling();
      if (status.step === 'done' && !status.error) {
        await updateServerStatus();
      }
    }
  } catch {}
}

async function pollRegenWorldStatus() {
  try {
    const status = await API.get('/api/server/regenerate-world/status');
    applyRegenProgress(status);
    if (!status.active) {
      stopRegenPolling();
      await updateServerStatus();
      if (status.error) {
        toast(status.error, 'error');
        if (status.details?.cleared?.length) {
          toast(`Архивировано элементов: ${status.details.cleared.length}`, 'info');
        }
      } else if (status.step === 'done') {
        toast(status.message || 'Мир пересобран', status.details?.warning ? 'warning' : 'success');
        if (status.details?.warning) {
          toast(status.details.warning, 'warning');
        }
      }
    }
  } catch {}
}

async function regenerateWorld() {
  if (!confirm(
    'Пересобрать мир? Кластер будет остановлен, сейвы (session/Save/backup) архивируются в *.regen.* и заменятся новым миром. Рекомендуется сделать полный бэкап.'
  )) return;
  try {
    toast('Пересборка мира запущена...', 'info');
    const data = await API.post('/api/server/regenerate-world');
    if (!data.success) {
      toast(data.error || 'Не удалось запустить пересборку', 'error');
      return;
    }
    applyRegenProgress(data.status);
    if (!regenPollInterval) {
      regenPollInterval = setInterval(pollRegenWorldStatus, 2000);
    }
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function updateServerStatus() {
  try {
    const data = await API.get('/api/server/status');
    applyServerStatus(data);
  } catch {}
}

async function clusterAction(action) {
  const labels = {
    start: 'Запуск кластера',
    stop: 'Остановка кластера',
    restart: 'Перезапуск кластера',
  };
  try {
    if (action === 'start' || action === 'restart') {
      const ready = await API.get('/api/server/cluster-readiness');
      if (!ready.ok) {
        toast('Сначала настройте конфиг: ' + (ready.errors || []).join('; '), 'error');
        navigateTo('config');
        return;
      }
      const worldReady = await API.get('/api/worlds/readiness');
      if (!worldReady.ready) {
        const failed = (worldReady.checks || []).filter(c => c.required && !c.ok);
        toast('Мир не готов: ' + failed.map(c => c.hint || c.label).join('; '), 'error');
        return;
      }
    }
    toast(`${labels[action]}...`, 'info');
    const data = await API.post(`/api/server/${action}-cluster`);
    await updateServerStatus();
    if (data.success) {
      toast(data.message || labels[action] + ' выполнен', 'success');
      if (data.caves?.warning) {
        toast(data.caves.warning, 'warning');
      }
      if (data.shard_health && !data.shard_health.caves_linked && data.caves?.success) {
        toast('Caves ещё подключается — подождите 1–2 мин и обновите статус', 'info');
      }
    } else {
      toast(data.error || data.message || 'Ошибка', 'error');
      if (data.config_on_disk) {
        const c = data.config_on_disk;
        toast(
          `cluster.ini: shard_enabled=${c.shard_enabled}, master_port=${c.master_port}`,
          'info'
        );
      }
      if (data.hints?.length) {
        toast(data.hints.join(' · '), 'info');
      }
      if (data.caves?.log_tail || data.master?.log_tail) {
        await showShardLogsOnError('Caves', data.caves?.log_tail || data.master?.log_tail);
      }
    }
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function serverAction(action, shard) {
  const labels = { start: 'Запуск', stop: 'Остановка', restart: 'Перезапуск' };
  try {
    if (action === 'start') {
      const ready = await API.get(`/api/server/readiness/${shard}`);
      if (!ready.ready) {
        toast(`Не готов к запуску ${shard}`, 'error');
        await updateServerStatus();
        return;
      }
    }
    toast(`${labels[action]} ${shard}...`, 'info');
    const data = await API.post(`/api/server/${action}/${shard}`);

    if (action === 'start' || action === 'restart') {
      if (!data.success) {
        toast(data.error || `Ошибка ${labels[action].toLowerCase()}а`, 'error');
        await showShardLogsOnError(shard, data.log_tail);
        await updateServerStatus();
        return;
      }
      const running = data.already_running || await waitForShardRunning(shard, 6);
      await updateServerStatus();
      if (running) {
        toast(data.message || `${shard} запущен`, 'success');
      } else {
        toast(`${shard}: процесс не удержался. Проверьте логи.`, 'error');
        await showShardLogsOnError(shard, data.log_tail);
      }
    } else {
      if (data.success) toast(`${shard} остановлен`, 'success');
      else toast(data.error || 'Ошибка остановки', 'error');
      await updateServerStatus();
    }
  } catch (err) { toast(err.message, 'error'); }
}

async function installServer() {
  try {
    toast('Установка DST-сервера... (это может занять время)', 'info');
    const data = await API.post('/api/server/install');
    if (data.success) toast('Установка завершена', 'success');
    else toast('Ошибка установки: ' + (data.error || 'неизвестная'), 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function updateServer() {
  try {
    toast('Обновление DST-сервера...', 'info');
    const data = await API.post('/api/server/update');
    if (data.success) toast('Обновление завершено', 'success');
    else toast('Ошибка обновления: ' + (data.error || 'неизвестная'), 'error');
  } catch (err) { toast(err.message, 'error'); }
}

// === CONFIG ===

async function renderConfig() {
  const el = $('page-config');
  let workflowHtml = '';
  try {
    const wf = await API.get('/api/config/workflow');
    workflowHtml = buildConfigWorkflowHtml(wf);
  } catch {}
  el.innerHTML = `
    ${pageHeader('settings-2', 'Конфигурация', 'Настройка кластера перед запуском')}
    ${workflowHtml}
    <div class="tabs">
      <div class="tab active" data-tab="cluster" onclick="switchConfigTab('cluster')"><i data-lucide="network"></i> Кластер</div>
      <div class="tab" data-tab="master" onclick="switchConfigTab('master')"><i data-lucide="sun"></i> Master</div>
      <div class="tab" data-tab="caves" onclick="switchConfigTab('caves')"><i data-lucide="mountain"></i> Caves</div>
      <div class="tab" data-tab="token" onclick="switchConfigTab('token')"><i data-lucide="key"></i> Токен</div>
      <div class="tab" data-tab="adminlist" onclick="switchConfigTab('adminlist')"><i data-lucide="shield"></i> Списки</div>
    </div>
    <div id="config-content"><div class="loading-center"><span class="spinner"></span></div></div>
  `;
  refreshIcons();
  switchConfigTab(pendingConfigTab || 'cluster');
  pendingConfigTab = null;
}

async function switchConfigTab(tab) {
  document.querySelectorAll('#page-config .tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`#page-config .tab[data-tab="${tab}"]`)?.classList.add('active');
  const cc = $('config-content');
  cc.innerHTML = '<div class="loading-center"><span class="spinner"></span></div>';
  refreshIcons();
  const editable = isOp();
  try {
    if (tab === 'cluster') {
      const data = await API.get('/api/config/cluster');
      cc.innerHTML = buildClusterForm(data, editable);
    } else if (tab === 'master') {
      const [data, binding] = await Promise.all([
        API.get('/api/config/shard/master'),
        API.get('/api/config/master/binding'),
      ]);
      cc.innerHTML = buildMasterForm(data, binding, editable);
    } else if (tab === 'caves') {
      const [data, binding] = await Promise.all([
        API.get('/api/config/shard/caves'),
        API.get('/api/config/caves/binding'),
      ]);
      cc.innerHTML = buildCavesForm(data, binding, editable);
    } else if (tab === 'token') {
      const data = await API.get('/api/config/token');
      const readonly = !isAdmin();
      const reqBadge = data.offline_mode
        ? '<span class="badge warn">Не нужен (офлайн)</span>'
        : (data.has_token ? '<span class="badge ok">Установлен</span>' : '<span class="badge err">Обязателен</span>');
      cc.innerHTML = `
        <div class="card">
          <div class="card-header"><i data-lucide="key"></i><h3>Cluster Token</h3></div>
          ${data.offline_mode ? `
            <p class="text-muted-sm">Режим <strong>офлайн-кластера</strong> включён — токен Klei не требуется. Игроки подключаются по IP/LAN.</p>
          ` : `
            <p class="text-muted-sm">Для <strong>онлайн-сервера</strong> нужен Cluster Token с
            <a href="${escAttr(data.klei_url)}" target="_blank" rel="noopener">accounts.klei.com</a>
            (нужна игра DST в аккаунте Klei).</p>
          `}
          <p class="text-muted">
            Токен хранится в <code>${escHtml(data.token_path)}</code> и не отображается после сохранения.
          </p>
          <div class="field">
            <label>Токен Klei</label>
            <input type="password" class="input" id="token-input" placeholder="Вставьте токен с сайта Klei" ${readonly ? 'readonly' : ''}>
          </div>
          <div class="btn-group">
            <button class="btn btn-primary btn-sm" onclick="saveToken()" ${readonly ? 'disabled' : ''}>
              <i data-lucide="save"></i> Сохранить токен
            </button>
          </div>
          <p class="text-muted-sm mt-12">
            Статус: ${reqBadge}
          </p>
        </div>
      `;
    } else if (tab === 'adminlist') {
      const data = await API.get('/api/config/lists');
      cc.innerHTML = buildListsPanel(data, isAdmin());
    }
    refreshIcons();
  } catch (err) {
    cc.innerHTML = '';
    renderLoadError(cc, err.message, null);
  }
}

function buildConfigWorkflowHtml(wf) {
  const steps = wf.steps || [];
  const ready = wf.ready_to_start;
  const activeIdx = steps.findIndex(s => !s.done);
  const currentIdx = activeIdx === -1 ? steps.length - 1 : activeIdx;
  return `
    <div class="card workflow-card">
      <div class="card-header">
        <i data-lucide="list-checks"></i>
        <h3>Порядок настройки</h3>
        ${ready
          ? '<span class="badge ok">Готов к запуску</span>'
          : '<span class="badge warn">Не завершено</span>'}
      </div>
      <div class="setup-progress">
        ${steps.map((s, i) => `
          <div class="setup-step ${s.done ? 'done' : ''} ${i === currentIdx && !s.done ? 'active' : ''}">
            <div class="setup-step-icon">
              <i data-lucide="${s.done ? 'check-circle' : (i === currentIdx ? 'circle-dot' : 'circle')}"></i>
            </div>
            <div class="setup-step-body">
              <strong>${escHtml(s.label)}</strong>
              <span>${s.done ? 'Готово' : escHtml(s.hint || 'Требуется настройка')}</span>
            </div>
          </div>
        `).join('')}
      </div>
      <div class="btn-group">
        <button class="btn btn-outline btn-sm" onclick="applyAllBindings()" ${isOp() ? '' : 'disabled'}>
          <i data-lucide="link"></i> Привязать всё автоматически
        </button>
      </div>
    </div>
  `;
}

async function applyAllBindings() {
  try {
    toast('Привязка конфигов...', 'info');
    const data = await API.post('/api/config/apply-bindings');
    if (data.success) {
      toast('Master и Caves привязаны к cluster.ini', 'success');
      renderConfig();
    } else {
      toast(data.error || 'Ошибка привязки', 'error');
    }
  } catch (err) {
    toast(err.message, 'error');
  }
}

function buildClusterForm(data, editable) {
  const fields = [
    ['GAMEPLAY.max_players', 'Макс. игроков', 'number'],
    ['GAMEPLAY.pvp', 'PvP', 'checkbox'],
    ['GAMEPLAY.game_mode', 'Режим игры', 'select', ['survival', 'endless', 'wilderness', 'lavaarena']],
    ['NETWORK.cluster_name', 'Название кластера', 'text'],
    ['NETWORK.cluster_description', 'Описание', 'text'],
    ['NETWORK.cluster_password', 'Пароль сервера', 'text'],
    ['NETWORK.offline_cluster', 'Офлайн-кластер (без токена Klei)', 'checkbox'],
    ['NETWORK.lan_only_cluster', 'Только LAN (не в браузере)', 'checkbox'],
    ['NETWORK.cluster_language', 'Язык', 'select', ['english', 'russian', 'chinese', 'portuguese', 'polish', 'spanish', 'german', 'french', 'italian', 'japanese', 'korean', 'dutch']],
    ['MISC.console_enabled', 'Консоль', 'checkbox'],
    ['MISC.tick_rate', 'Tick Rate', 'number'],
    ['SHARD.shard_enabled', 'Шарды (пещеры)', 'checkbox'],
    ['SHARD.bind_ip', 'Bind IP', 'text'],
    ['SHARD.master_ip', 'Master IP', 'text'],
    ['SHARD.master_port', 'Порт шардов', 'number'],
    ['SHARD.cluster_key', 'Ключ кластера', 'text'],
  ];
  let html = `
    <div class="card">
      <div class="card-header"><i data-lucide="file-cog"></i><h3>cluster.ini</h3></div>
      <p class="step-hint">
        <strong>Шаг 1.</strong> Сохраните cluster.ini (имя сервера, shard_enabled=true, ключ кластера).
        Порты по умолчанию: Master 10999, Caves 11000, связь шардов 10888.
      </p>
      <div class="btn-group">
        <button class="btn btn-primary btn-sm" onclick="applyPresetFromConfig('online')" ${editable ? '' : 'disabled'}>
          <i data-lucide="globe"></i> Пресет «онлайн»
        </button>
        <button class="btn btn-outline btn-sm" onclick="applyPresetFromConfig('friends')" ${editable ? '' : 'disabled'}>
          <i data-lucide="users"></i> Пресет «офлайн»
        </button>
      </div>
  `;
  for (const [key, label, type, opts] of fields) {
    html += buildField(key, label, type, data[key] || '', editable, opts);
  }
  html += `<div class="btn-group"><button class="btn btn-primary btn-sm" onclick="saveClusterConfig()" ${editable ? '' : 'disabled'}><i data-lucide="save"></i> Сохранить cluster.ini</button></div></div>`;
  return html;
}

function buildMasterForm(data, binding, editable) {
  const m = binding.master || {};
  const cl = binding.cluster || {};
  const cv = binding.caves || {};
  const syncBadge = m.synced
    ? '<span class="badge ok">Синхронизировано</span>'
    : '<span class="badge warn">Требует привязки</span>';
  const clusterBadge = cl.exists
    ? `<span class="badge ok">${escHtml(cl.name || 'cluster.ini')}</span>`
    : '<span class="badge err">Нет cluster.ini</span>';
  const tokenBadge = cl.offline
    ? '<span class="badge warn">Офлайн — не нужен</span>'
    : (cl.token_ok ? '<span class="badge ok">Есть</span>' : '<span class="badge err">Нужен</span>');
  const runBadge = m.running
    ? `<span class="badge ok">Запущен</span> PID ${escHtml(String(m.pid || '—'))}`
    : '<span class="badge">Остановлен</span>';

  const masterFields = [
    ['NETWORK.server_port', 'Игровой порт (UDP)', 'number'],
    ['SHARD.is_master', 'Главный шард', 'checkbox'],
    ['SHARD.name', 'Имя шарда', 'text'],
    ['SHARD.id', 'ID шарда', 'number'],
    ['STEAM.master_server_port', 'Steam master port', 'number'],
    ['STEAM.authentication_port', 'Steam auth port', 'number'],
    ['ACCOUNT.encode_user_path', 'Кодировать путь', 'checkbox'],
  ];

  let html = `
    <div class="card card-accent">
      <div class="card-header"><i data-lucide="link-2"></i><h3>Привязка к cluster.ini</h3></div>
      <div class="info-grid">
        <div class="info-item"><label>cluster.ini</label><div class="val">${clusterBadge}</div></div>
        <div class="info-item"><label>Синхронизация</label><div class="val">${syncBadge}</div></div>
        <div class="info-item"><label>Токен Klei</label><div class="val">${tokenBadge}</div></div>
        <div class="info-item"><label>Статус</label><div class="val">${runBadge}</div></div>
        <div class="info-item"><label>Bind IP</label><div class="val">${escHtml(cl.bind_ip || '0.0.0.0')}</div></div>
        <div class="info-item"><label>Порт шардов</label><div class="val">${escHtml(cl.master_shard_port || '10888')}</div></div>
        <div class="info-item"><label>Master IP</label><div class="val">${escHtml(cl.master_ip || '127.0.0.1')}</div></div>
        <div class="info-item"><label>Порт Caves</label><div class="val">${escHtml(cv.game_port || '11000')}</div></div>
      </div>
      <p class="text-muted-sm mt-12">
        <strong>Шаг 2.</strong> Master слушает шарды на
        <strong>${escHtml(cl.master_ip || '127.0.0.1')}:${escHtml(cl.master_shard_port || '10888')}</strong>.
        Игровой порт: <strong>${escHtml(m.game_port || '10999')}</strong> (≠ Caves ${escHtml(cv.game_port || '11000')}).
      </p>
      ${!binding.ports_ok ? `<p class="text-muted-sm mt-12"><i data-lucide="alert-triangle" class="icon-inline"></i> Порты Master и Caves совпадают — привязка исправит это.</p>` : ''}
      <div class="btn-group mt-12">
        <button class="btn btn-primary btn-sm" onclick="bindMasterToCluster()" ${editable && cl.exists ? '' : 'disabled'}>
          <i data-lucide="link"></i> Привязать к cluster.ini
        </button>
        <button class="btn btn-outline btn-sm" onclick="openConfigTab('cluster')">
          <i data-lucide="file-cog"></i> Кластер
        </button>
      </div>
      ${!cl.exists ? `<p class="text-muted-sm mt-12">Сначала создайте конфиг кластера (пресет на «Запуск»).</p>` : ''}
      ${!cl.offline && !cl.token_ok ? `<p class="text-muted-sm mt-12"><i data-lucide="key" class="icon-inline"></i> Для онлайн-режима сохраните токен Klei.</p>` : ''}
    </div>
    <div class="card">
      <div class="card-header"><i data-lucide="sun"></i><h3>Master/server.ini</h3></div>
      <div class="btn-group">
        <button class="btn btn-outline btn-sm" onclick="applyPresetFromConfig('online')" ${editable ? '' : 'disabled'}>
          <i data-lucide="globe"></i> Пресет «онлайн»
        </button>
        <button class="btn btn-outline btn-sm" onclick="applyPresetFromConfig('friends')" ${editable ? '' : 'disabled'}>
          <i data-lucide="users"></i> Пресет «офлайн»
        </button>
      </div>`;

  for (const [key, label, type] of masterFields) {
    html += buildField(key, label, type, data[key] || '', editable);
  }
  html += `<div class="btn-group"><button class="btn btn-primary btn-sm" onclick="saveShardConfig('master')" ${editable ? '' : 'disabled'}><i data-lucide="save"></i> Сохранить Master/server.ini</button></div></div>`;
  return html;
}

async function bindMasterToCluster() {
  try {
    toast('Привязка Master к cluster.ini...', 'info');
    const data = await API.post('/api/config/master/bind-cluster');
    if (data.success) {
      toast(data.message || 'Master привязан. Далее: вкладка Caves → привязка.', 'success');
      renderConfig();
    } else {
      toast(data.error || 'Ошибка привязки', 'error');
    }
  } catch (err) { toast(err.message, 'error'); }
}

function buildCavesForm(data, binding, editable) {
  const m = binding.master || {};
  const cl = binding.cluster || {};
  const cv = binding.caves || {};
  const masterBadge = m.synced
    ? '<span class="badge ok">Привязан</span>'
    : '<span class="badge warn">Не привязан</span>';
  const syncBadge = cv.synced
    ? '<span class="badge ok">Синхронизировано</span>'
    : '<span class="badge warn">Требует привязки</span>';
  const masterSyncBadge = m.synced
    ? '<span class="badge ok">cluster.ini OK</span>'
    : '<span class="badge warn">Master не синхр.</span>';

  const cavesFields = [
    ['NETWORK.server_port', 'Игровой порт (UDP)', 'number'],
    ['SHARD.is_master', 'Главный шард', 'checkbox'],
    ['SHARD.name', 'Имя шарда', 'text'],
    ['SHARD.id', 'ID шарда', 'number'],
    ['SHARD.master_ip', 'IP Master (связь шардов)', 'text'],
    ['SHARD.master_port', 'Порт связи с Master', 'number'],
    ['STEAM.master_server_port', 'Steam master port', 'number'],
    ['STEAM.authentication_port', 'Steam auth port', 'number'],
    ['ACCOUNT.encode_user_path', 'Кодировать путь', 'checkbox'],
  ];

  let html = `
    <div class="card card-accent">
      <div class="card-header"><i data-lucide="link-2"></i><h3>Привязка к Master</h3></div>
      <div class="info-grid">
        <div class="info-item"><label>Master</label><div class="val">${masterBadge}</div></div>
        <div class="info-item"><label>Синхронизация Caves</label><div class="val">${syncBadge}</div></div>
        <div class="info-item"><label>Master IP (cluster.ini)</label><div class="val">${escHtml(cl.master_ip || '127.0.0.1')}</div></div>
        <div class="info-item"><label>Caves master_ip</label><div class="val">${escHtml(cv.master_ip || '—')}</div></div>
        <div class="info-item"><label>Порт шардов</label><div class="val">${escHtml(cl.master_shard_port || '10888')}</div></div>
        <div class="info-item"><label>Порт Master (игра)</label><div class="val">${escHtml(m.game_port || '10999')}</div></div>
        <div class="info-item"><label>Ключ кластера</label><div class="val">${escHtml(cl.cluster_key || 'default')}</div></div>
      </div>
      <p class="text-muted-sm mt-12">
        <strong>Шаг 3.</strong> Caves подключается к Master по
        <strong>${escHtml(cl.master_ip || '127.0.0.1')}:${escHtml(cl.master_shard_port || '10888')}</strong>
        (поля SHARD.master_ip / master_port в Caves/server.ini).
        Запуск — только через «Запустить кластер» на вкладке Сервер.
      </p>
      <div class="btn-group mt-12">
        <button class="btn btn-primary btn-sm" onclick="bindCavesToMaster()" ${editable && m.synced ? '' : 'disabled'}>
          <i data-lucide="link"></i> Привязать к Master
        </button>
        <button class="btn btn-outline btn-sm" onclick="openConfigTab('master')">
          <i data-lucide="sun"></i> Master
        </button>
      </div>
      ${!m.synced ? `<p class="text-muted-sm mt-12"><i data-lucide="alert-triangle" class="icon-inline"></i> Сначала шаг 2: привяжите Master к cluster.ini.</p>` : ''}
    </div>
    <div class="card">
      <div class="card-header"><i data-lucide="mountain"></i><h3>Caves/server.ini</h3></div>
      <div class="btn-group">
        <button class="btn btn-outline btn-sm" onclick="applyPresetFromConfig('online')" ${editable ? '' : 'disabled'}>
          <i data-lucide="globe"></i> Пресет «онлайн»
        </button>
        <button class="btn btn-outline btn-sm" onclick="applyPresetFromConfig('friends')" ${editable ? '' : 'disabled'}>
          <i data-lucide="users"></i> Пресет «офлайн»
        </button>
      </div>`;

  for (const [key, label, type] of cavesFields) {
    html += buildField(key, label, type, data[key] || '', editable);
  }
  html += `<div class="btn-group"><button class="btn btn-primary btn-sm" onclick="saveShardConfig('caves')" ${editable ? '' : 'disabled'}><i data-lucide="save"></i> Сохранить Caves/server.ini</button></div></div>`;
  return html;
}

async function bindCavesToMaster() {
  try {
    toast('Привязка пещер к Master...', 'info');
    const data = await API.post('/api/config/caves/bind-master');
    if (data.success) {
      toast(data.message || 'Caves привязан. Можно запускать кластер на вкладке Сервер.', 'success');
      renderConfig();
    } else {
      toast(data.error || 'Ошибка привязки', 'error');
      if (!data.binding?.master?.running) switchConfigTab('caves');
    }
  } catch (err) { toast(err.message, 'error'); }
}

async function applyPresetFromConfig(mode) {
  const isFriends = mode === 'friends';
  const defaultName = isFriends ? 'Игра с друзьями' : 'Мой DST сервер';
  const name = prompt('Название сервера:', defaultName);
  if (name === null) return;
  const password = prompt('Пароль (оставьте пустым, если не нужен):', '') ?? '';
  const label = isFriends ? 'офлайн' : 'онлайн';
  if (!confirm(`Перезаписать все конфиги (${label}-пресет)?`)) return;
  try {
    const data = await API.post(`/api/server/preset/${mode}`, { cluster_name: name.trim() || defaultName, password });
    if (data.success) {
      const a = data.applied || {};
      toast(`Пресет ${label}: Master :${a.master_port}, Caves :${a.caves_port}`, 'success');
      const activeTab = document.querySelector('#page-config .tab.active')?.dataset?.tab || 'cluster';
      switchConfigTab(activeTab);
    } else {
      toast('Ошибка: ' + (data.error || 'неизвестная'), 'error');
    }
  } catch (err) { toast(err.message, 'error'); }
}

async function saveClusterConfig() {
  const data = {};
  document.querySelectorAll('#config-content [data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.type === 'checkbox') data[key] = el.checked ? 'true' : 'false';
    else data[key] = el.value;
  });
  try {
    await API.put('/api/config/cluster', { data });
    toast('cluster.ini сохранён. Далее: вкладка Master → привязка.', 'success');
    renderConfig();
  } catch (err) { toast(err.message, 'error'); }
}

async function saveShardConfig(name) {
  const data = {};
  document.querySelectorAll('#config-content [data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.type === 'checkbox') data[key] = el.checked ? 'true' : 'false';
    else data[key] = el.value;
  });
  try {
    await API.put(`/api/config/shard/${name}`, { data });
    toast(`${name}/server.ini сохранён`, 'success');
  } catch (err) { toast(err.message, 'error'); }
}

async function saveToken() {
  const token = $('token-input').value;
  if (!token) { toast('Введите токен', 'error'); return; }
  try {
    await API.put('/api/config/token', { token });
    toast('Токен сохранён', 'success');
    $('token-input').value = '';
    switchConfigTab('token');
  } catch (err) { toast(err.message, 'error'); }
}

async function saveFile(filename) {
  const content = $(`file-${filename}`).value;
  try {
    await API.put(`/api/config/file/${filename}`, { content });
    toast(`${filename} сохранён`, 'success');
  } catch (err) { toast(err.message, 'error'); }
}

function fmtPlaytime(sec) {
  return fmtUptime(sec || 0);
}

function fmtDateTime(iso) {
  if (!iso) return '—';
  return escHtml(iso.replace('T', ' ').slice(0, 16));
}

function buildDailyChart(daily, maxBars = 14) {
  const items = (daily || []).slice(-maxBars);
  if (!items.length) return '<p class="text-muted-sm">Нет данных за период</p>';
  const maxSec = Math.max(...items.map(d => d.playtime_seconds || 0), 1);
  return `
    <div class="activity-chart">
      ${items.map(d => {
        const pct = Math.max(4, Math.round(((d.playtime_seconds || 0) / maxSec) * 100));
        const label = (d.date || '').slice(5);
        const tip = `${d.date}: ${fmtPlaytime(d.playtime_seconds)} · ${d.session_count || 0} сесс.`;
        return `
          <div class="activity-bar" title="${escAttr(tip)}">
            <div class="activity-bar-fill" style="height:${pct}%"></div>
            <span class="activity-bar-label">${escHtml(label)}</span>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function buildPlayersDashboard(dashboard) {
  const d = dashboard || {};
  const top = (d.top_players || []).map(p => `
    <div class="top-player-row">
      <span>${escHtml(p.name)}</span>
      <span class="text-muted-sm">${escHtml(p.total_playtime_human || fmtPlaytime(p.total_playtime_seconds))}</span>
    </div>
  `).join('') || '<p class="text-muted-sm">Пока нет данных</p>';

  return `
    <div class="grid-4 players-dash-grid">
      <div class="card card-stat">
        <div class="card-header"><i data-lucide="users"></i><h3>Всего игроков</h3></div>
        <div class="value">${d.unique_players ?? '—'}</div>
      </div>
      <div class="card card-stat">
        <div class="card-header"><i data-lucide="radio"></i><h3>Онлайн</h3></div>
        <div class="value">${d.online_count ?? 0}</div>
      </div>
      <div class="card card-stat">
        <div class="card-header"><i data-lucide="clock"></i><h3>Сегодня в игре</h3></div>
        <div class="value">${fmtPlaytime(d.playtime_today_seconds || 0)}</div>
        <div class="sub">${d.sessions_today || 0} сессий</div>
      </div>
      <div class="card card-stat">
        <div class="card-header"><i data-lucide="history"></i><h3>Всего сессий</h3></div>
        <div class="value">${d.total_sessions ?? 0}</div>
        <div class="sub">${fmtPlaytime(d.total_playtime_seconds || 0)} суммарно</div>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-header"><i data-lucide="bar-chart-3"></i><h3>Активность по дням (14 дн.)</h3></div>
        ${buildDailyChart(d.daily_activity)}
      </div>
      <div class="card">
        <div class="card-header"><i data-lucide="trophy"></i><h3>Топ по времени в игре</h3></div>
        <div class="top-players-list">${top}</div>
      </div>
    </div>
  `;
}

function playerRoleBadges(roles, online) {
  const parts = [];
  if (online) parts.push('<span class="badge ok">онлайн</span>');
  if (roles?.admin) parts.push('<span class="badge ok">админ</span>');
  if (roles?.banned) parts.push('<span class="badge err">бан</span>');
  if (roles?.whitelisted) parts.push('<span class="badge warn">whitelist</span>');
  return parts.join(' ') || '<span class="badge">—</span>';
}

function playerActionButtons(kleiId, roles, canEdit) {
  if (!canEdit) return '';
  const id = escAttr(kleiId);
  let html = '<div class="list-row-actions">';
  if (roles?.admin) {
    html += `<button class="btn btn-outline btn-sm" onclick="listAction('${id}','remove_admin')" title="Снять админку"><i data-lucide="shield-off"></i></button>`;
  } else {
    html += `<button class="btn btn-outline btn-sm" onclick="listAction('${id}','add_admin')" title="Выдать админку"><i data-lucide="shield-check"></i></button>`;
  }
  if (roles?.banned) {
    html += `<button class="btn btn-outline btn-sm" onclick="listAction('${id}','remove_ban')" title="Разбанить"><i data-lucide="user-check"></i></button>`;
  } else {
    html += `<button class="btn btn-outline btn-sm btn-danger" onclick="listAction('${id}','add_ban')" title="Забанить"><i data-lucide="ban"></i></button>`;
  }
  if (roles?.whitelisted) {
    html += `<button class="btn btn-outline btn-sm" onclick="listAction('${id}','remove_whitelist')" title="Убрать из whitelist"><i data-lucide="user-minus"></i></button>`;
  } else {
    html += `<button class="btn btn-outline btn-sm" onclick="listAction('${id}','add_whitelist')" title="В whitelist"><i data-lucide="user-plus"></i></button>`;
  }
  html += '</div>';
  return html.replace('class="list-row-actions"', 'class="list-row-actions" onclick="event.stopPropagation()"');
}

function buildPlayerRow(player, canEdit, showMeta, clickable = false) {
  const meta = showMeta
    ? `<span class="text-muted-sm">
        ${player.online ? 'В игре' : 'Офлайн'}
        · ${player.session_count || 0} сесс.
        · ${escHtml(player.total_playtime_human || fmtPlaytime(player.total_playtime_seconds))}
        ${player.first_ip ? ' · IP ' + escHtml(player.last_ip || player.first_ip) : ''}
      </span>`
    : `<span class="text-muted-sm">${escHtml(player.klei_id)}${player.ip_address ? ' · ' + escHtml(player.ip_address) : ''}${player.since ? ' · с ' + escHtml(player.since.replace('T', ' ').slice(11, 16)) : ''}</span>`;
  const clickAttr = clickable
    ? ` class="list-row list-row--clickable" onclick="openPlayerDetail('${escAttr(player.klei_id)}')"`
    : ` class="list-row"`;
  return `
    <div${clickAttr}>
      <div class="list-row-main">
        <strong>${escHtml(player.name || player.klei_id)}</strong>
        ${meta}
      </div>
      <div class="list-row-meta">${playerRoleBadges(player.roles, player.online)}</div>
      ${playerActionButtons(player.klei_id, player.roles, canEdit)}
    </div>
  `;
}

async function openPlayerDetail(kleiId) {
  if (!kleiId) return;
  try {
    toast('Загрузка профиля...', 'info');
    const data = await API.get(`/api/server/players/${encodeURIComponent(kleiId)}`);
    const p = data.player;
    const sessions = (data.sessions || []).slice(0, 15).map(s => `
      <tr>
        <td>${fmtDateTime(s.started_at)}</td>
        <td>${s.active ? '<span class="badge ok">в игре</span>' : fmtDateTime(s.ended_at)}</td>
        <td>${escHtml(s.duration_human || fmtPlaytime(s.duration_seconds))}</td>
        <td>${escHtml(s.ip_address || '—')}</td>
        <td>${escHtml(s.shard || '—')}</td>
      </tr>
    `).join('') || '<tr><td colspan="5" class="text-muted">Нет сессий</td></tr>';

    showModal(p.name || p.klei_id, `
      <div class="info-grid info-grid--compact">
        <div class="info-item"><label>Klei ID</label><div class="val code-inline">${escHtml(p.klei_id)}</div></div>
        <div class="info-item"><label>Первый вход</label><div class="val">${fmtDateTime(p.first_seen)}</div></div>
        <div class="info-item"><label>Последний онлайн</label><div class="val">${fmtDateTime(p.last_seen)}</div></div>
        <div class="info-item"><label>Первый IP</label><div class="val">${escHtml(p.first_ip || '—')}</div></div>
        <div class="info-item"><label>Последний IP</label><div class="val">${escHtml(p.last_ip || '—')}</div></div>
        <div class="info-item"><label>Сессий</label><div class="val">${p.session_count || 0}</div></div>
        <div class="info-item"><label>Время в игре</label><div class="val">${escHtml(p.total_playtime_human || fmtPlaytime(p.total_playtime_seconds))}</div></div>
        <div class="info-item"><label>Статус</label><div class="val">${playerRoleBadges(p.roles, p.online)}</div></div>
      </div>
      <p class="section-label mt-16">Активность по дням (30 дн.)</p>
      ${buildDailyChart(data.daily, 30)}
      <p class="section-label mt-16">Игровые сессии</p>
      <div class="table-wrap">
        <table class="data-table data-table--compact">
          <thead><tr><th>Начало</th><th>Конец</th><th>Длительность</th><th>IP</th><th>Шард</th></tr></thead>
          <tbody>${sessions}</tbody>
        </table>
      </div>
      <div class="btn-group mt-16">
        <button class="btn btn-outline btn-sm" onclick="closeModal()">Закрыть</button>
      </div>
    `);
  } catch (err) {
    toast(err.message, 'error');
  }
}

function buildListsPanel(data, canEdit) {
  const players = data.players || [];
  const online = data.online || [];
  const listSummary = `
    <div class="info-grid info-grid--compact">
      <div class="info-item"><label>Админы</label><div class="val">${(data.admin || []).length}</div></div>
      <div class="info-item"><label>Бан</label><div class="val">${(data.block || []).length}</div></div>
      <div class="info-item"><label>Whitelist</label><div class="val">${(data.whitelist || []).length}</div></div>
      <div class="info-item"><label>Онлайн</label><div class="val">${data.online_count || 0}</div></div>
    </div>
  `;

  const onlineRows = online.length
    ? online.map(p => buildPlayerRow({ ...p, online: true, roles: p.roles }, canEdit, false)).join('')
    : '<p class="text-muted">Сейчас никого нет на сервере. Игроки появятся после входа (нужен запущенный Master).</p>';

  const knownRows = players.length
    ? players.map(p => buildPlayerRow(p, canEdit, true, false)).join('')
    : '<p class="text-muted">Пока нет известных игроков. Они появятся в логах после первого входа.</p>';

  return `
    <p class="text-muted-sm">${escHtml(data.note || '')} Klei ID:
      <a href="https://accounts.klei.com/account/profile" target="_blank" rel="noopener">accounts.klei.com</a>
    </p>
    ${!data.log_available ? `
      <p class="text-muted-sm mt-12">
        <i data-lucide="alert-triangle" class="icon-inline"></i>
        Лог Master ещё не создан — запустите сервер, чтобы видеть игроков из логов.
      </p>
    ` : ''}
    <div class="card">
      <div class="card-header"><i data-lucide="shield"></i><h3>Списки доступа</h3></div>
      <p class="text-muted-sm">${escHtml(readListsNote(data))}</p>
      ${listSummary}
      <div class="input-row mt-12">
        <input type="text" class="input" id="list-manual-id" placeholder="Klei ID (KU_...)" ${canEdit ? '' : 'readonly'}>
        <button class="btn btn-outline btn-sm" onclick="listAction($('list-manual-id').value.trim(),'add_admin')" ${canEdit ? '' : 'disabled'}><i data-lucide="shield-check"></i> Админ</button>
        <button class="btn btn-outline btn-sm btn-danger" onclick="listAction($('list-manual-id').value.trim(),'add_ban')" ${canEdit ? '' : 'disabled'}><i data-lucide="ban"></i> Бан</button>
        <button class="btn btn-outline btn-sm" onclick="listAction($('list-manual-id').value.trim(),'add_whitelist')" ${canEdit ? '' : 'disabled'}><i data-lucide="user-plus"></i> Whitelist</button>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><i data-lucide="radio"></i><h3>Сейчас на сервере</h3></div>
      <div class="list-rows">${onlineRows}</div>
    </div>
    <div class="card">
      <div class="card-header"><i data-lucide="users"></i><h3>Известные игроки</h3></div>
      <p class="text-muted-sm">Кликайте кнопки справа, чтобы выдать админку, забанить или добавить в whitelist.</p>
      <div class="list-rows">${knownRows}</div>
      <div class="btn-group">
        <button class="btn btn-outline btn-sm" onclick="navigateTo('players')"><i data-lucide="gamepad-2"></i> Подробнее на вкладке «Игроки»</button>
      </div>
    </div>
    <details class="card card-collapsible">
      <summary><i data-lucide="file-text"></i> Редактор файлов списков (расширенный)</summary>
      <div class="card-body-inner">
        ${[
          { fname: 'adminlist.txt', key: 'admin' },
          { fname: 'blocklist.txt', key: 'block' },
          { fname: 'whitelist.txt', key: 'whitelist' },
        ].map(({ fname, key }) => `
          <div class="field">
            <label>${escHtml(fname)}</label>
            <textarea id="file-${fname}" class="code code--sm">${escHtml((data[key] || []).join('\n'))}</textarea>
            <button class="btn btn-primary btn-sm" onclick="saveFile('${fname}')" ${canEdit ? '' : 'disabled'}>
              <i data-lucide="save"></i> Сохранить ${escHtml(fname)}
            </button>
          </div>
        `).join('')}
        <p class="text-muted-sm">Обычно достаточно кнопок выше. Ручное редактирование — для особых случаев.</p>
      </div>
    </details>
  `;
}

function readListsNote(data) {
  return data.note || 'Изменения вступают в силу после перезапуска шардов.';
}

async function listAction(kleiId, action) {
  if (!kleiId) { toast('Укажите Klei ID', 'error'); return; }
  if (action === 'add_ban' && !confirm(`Забанить игрока ${kleiId}?`)) return;
  try {
    const data = await API.post('/api/config/lists/action', { klei_id: kleiId, action });
    toast(data.message || 'Готово', 'success');
    if ($('page-config') && !$('page-config').classList.contains('hidden')) {
      switchConfigTab('adminlist');
    }
    if ($('page-players') && !$('page-players').classList.contains('hidden')) {
      renderPlayers();
    }
  } catch (err) { toast(err.message, 'error'); }
}

async function renderPlayers(silent) {
  const el = $('page-players');
  if (!silent) {
    el.innerHTML = `<div class="loading-center"><span class="spinner"></span></div>`;
    refreshIcons();
    stopPlayersPolling();
  }
  try {
    const data = await API.get('/api/server/players');
    const canEdit = isAdmin();
    const onlineRows = (data.online || []).map(p => buildPlayerRow({ ...p, online: true }, canEdit, false, false)).join('')
      || '<p class="text-muted">Сейчас никого нет на сервере.</p>';
    const allRows = (data.players || []).map(p => buildPlayerRow(p, canEdit, true, true)).join('')
      || '<p class="text-muted">Игроки появятся после первого входа на сервер.</p>';

    if (!silent && !data.log_available) {
      notice('Лог server_log.txt не найден. Запустите Master, чтобы отслеживать игроков.', 'warning');
    }

    el.innerHTML = `
      ${pageHeader('gamepad-2', 'Игроки', 'Сессии, статистика и управление доступом')}
      <div class="toolbar">
        <button class="btn btn-primary btn-sm" onclick="renderPlayers()"><i data-lucide="refresh-cw"></i> Обновить</button>
        <span class="text-muted-sm">Автообновление каждые 12 сек · клик по игроку — подробная статистика</span>
      </div>
      ${data.note ? `<p class="text-muted-sm">${escHtml(data.note)}</p>` : ''}
      ${buildPlayersDashboard(data.dashboard)}
      <div class="grid-2">
        <div class="card">
          <div class="card-header"><i data-lucide="radio"></i><h3>Онлайн сейчас (${data.online_count || 0})</h3></div>
          <div class="list-rows">${onlineRows}</div>
        </div>
        <div class="card">
          <div class="card-header"><i data-lucide="shield"></i><h3>Списки</h3></div>
          <div class="info-grid info-grid--compact">
            <div class="info-item"><label>Админы</label><div class="val">${(data.lists?.admin || []).length}</div></div>
            <div class="info-item"><label>Бан</label><div class="val">${(data.lists?.block || []).length}</div></div>
            <div class="info-item"><label>Whitelist</label><div class="val">${(data.lists?.whitelist || []).length}</div></div>
          </div>
          <div class="input-row mt-12">
            <input type="text" class="input" id="players-manual-id" placeholder="Klei ID" ${canEdit ? '' : 'readonly'}>
            <button class="btn btn-outline btn-sm" onclick="listAction($('players-manual-id').value.trim(),'add_admin')" ${canEdit ? '' : 'disabled'}>Админ</button>
            <button class="btn btn-outline btn-sm btn-danger" onclick="listAction($('players-manual-id').value.trim(),'add_ban')" ${canEdit ? '' : 'disabled'}>Бан</button>
          </div>
          <p class="text-muted-sm mt-12">Полное управление списками — в <a href="#" onclick="openConfigTab('adminlist');return false;">Конфиг → Списки</a>.</p>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><i data-lucide="history"></i><h3>Все игроки (когда-либо заходили)</h3></div>
        <p class="text-muted-sm">Нажмите на строку, чтобы открыть сессии, IP и график по дням.</p>
        <div class="list-rows">${allRows}</div>
      </div>
    `;
    refreshIcons();
    if (!silent) flushNotices();
    if (!playersPollInterval) {
      playersPollInterval = setInterval(() => {
        if (!$('page-players')?.classList.contains('hidden')) renderPlayers(true);
      }, PLAYERS_POLL_MS);
    }
  } catch (err) {
    if (!silent) renderLoadError(el, err.message, 'renderPlayers');
  }
}

// === MODS ===

function modStatusBadge(mod) {
  const parts = [];
  if (mod.enabled) parts.push('<span class="badge ok">включён</span>');
  else parts.push('<span class="badge">выключен</span>');
  if (mod.downloaded) parts.push('<span class="badge ok">скачан</span>');
  else parts.push('<span class="badge warn">не скачан</span>');
  return parts.join(' ');
}

async function renderMods() {
  const el = $('page-mods');
  el.innerHTML = `<div class="loading-center"><span class="spinner"></span></div>`;
  refreshIcons();
  try {
    const data = await API.get('/api/config/mods');
    const modsList = (data.mods || []).map(mod => `
      <div class="list-row">
        <div class="list-row-main">
          <strong>${escHtml(mod.title)}</strong>
          <span class="text-muted-sm">ID ${escHtml(mod.workshop_id)}</span>
        </div>
        <div class="list-row-meta">${modStatusBadge(mod)}</div>
        <div class="list-row-actions">
          <a class="btn btn-outline btn-sm" href="https://steamcommunity.com/sharedfiles/filedetails/?id=${escAttr(mod.workshop_id)}" target="_blank" rel="noopener">
            <i data-lucide="external-link"></i>
          </a>
          <button class="btn btn-outline btn-sm" onclick="removeMod('${escAttr(mod.workshop_id)}')" ${isAdmin() ? '' : 'disabled'}>
            <i data-lucide="trash-2"></i>
          </button>
        </div>
      </div>
    `).join('') || '<p class="text-muted">Моды не настроены. Добавьте Workshop ID или импортируйте коллекцию.</p>';

    const steamNote = data.steam_api_configured
      ? 'Steam Web API подключён — названия модов и импорт коллекций доступны.'
      : 'Для названий модов и импорта коллекций добавьте STEAM_WEB_API_KEY в .env панели (<a href="https://steamcommunity.com/dev/apikey" target="_blank" rel="noopener">получить ключ</a>).';

    el.innerHTML = `
      ${pageHeader('puzzle', 'Моды', 'Автоматическая установка через Steam Workshop')}
      ${data.note ? `<p class="text-muted-sm">${escHtml(data.note)}</p>` : ''}
      <div class="card">
        <div class="card-header"><i data-lucide="list"></i><h3>Установленные моды</h3></div>
        <p class="text-muted-sm">${steamNote}</p>
        <div class="list-rows">${modsList}</div>
        <p class="text-muted-sm">Файл скачивания: <code class="code-inline">${escHtml(data.setup_path || '')}</code></p>
        <p class="text-muted-sm">После изменений перезапустите Master и Caves — DST скачает моды при старте.</p>
      </div>
      <div class="card">
        <div class="card-header"><i data-lucide="plus-circle"></i><h3>Добавить мод</h3></div>
        <p class="text-muted-sm">ID из URL: steamcommunity.com/sharedfiles/filedetails/?id=<strong>378160973</strong></p>
        <div class="input-row">
          <input type="text" class="input" id="mod-workshop-id" placeholder="Workshop ID">
          <button class="btn btn-primary btn-sm" onclick="quickAddMod()" ${isAdmin() ? '' : 'disabled'}>
            <i data-lucide="plus"></i> Добавить
          </button>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><i data-lucide="layers"></i><h3>Импорт коллекции Workshop</h3></div>
        <p class="text-muted-sm">ID коллекции из URL: steamcommunity.com/workshop/filedetails/?id=<strong>1234567890</strong></p>
        <div class="input-row">
          <input type="text" class="input" id="mod-collection-id" placeholder="Collection ID">
          <button class="btn btn-primary btn-sm" onclick="importModCollection()" ${isAdmin() ? '' : 'disabled'}>
            <i data-lucide="download"></i> Импортировать
          </button>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><i data-lucide="file-code"></i><h3>modoverrides.lua (расширенный)</h3></div>
        <p class="text-muted-sm">Синхронизируется в Master/ и Caves/. Для настроек мода редактируйте блок мода вручную.</p>
        <textarea id="mods-editor" class="code">${escHtml(data.overrides_content || '')}</textarea>
        <div class="btn-group">
          <button class="btn btn-primary btn-sm" onclick="saveMods()" ${isAdmin() ? '' : 'disabled'}>
            <i data-lucide="save"></i> Сохранить overrides
          </button>
        </div>
      </div>
    `;
    refreshIcons();
  } catch (err) {
    renderLoadError(el, err.message, 'renderMods');
  }
}

async function saveMods() {
  try {
    const data = await API.put('/api/config/mods', { content: $('mods-editor').value });
    toast(data.message || 'modoverrides.lua сохранён', 'success');
    renderMods();
  } catch (err) { toast(err.message, 'error'); }
}

async function quickAddMod() {
  const id = $('mod-workshop-id').value.trim();
  if (!id || !/^\d+$/.test(id)) { toast('Некорректный Workshop ID', 'error'); return; }
  try {
    const data = await API.post('/api/config/mods/add', { workshop_id: id });
    toast(data.message || 'Мод добавлен', 'success');
    renderMods();
  } catch (err) { toast(err.message, 'error'); }
}

async function importModCollection() {
  const id = $('mod-collection-id').value.trim();
  if (!id || !/^\d+$/.test(id)) { toast('Некорректный ID коллекции', 'error'); return; }
  try {
    const data = await API.post('/api/config/mods/collection', { collection_id: id });
    toast(data.message || 'Коллекция импортирована', 'success');
    renderMods();
  } catch (err) { toast(err.message, 'error'); }
}

async function removeMod(workshopId) {
  if (!confirm(`Удалить мод ${workshopId} из конфигурации?`)) return;
  try {
    const data = await API.del(`/api/config/mods/${workshopId}`);
    toast(data.message || 'Мод удалён', 'success');
    renderMods();
  } catch (err) { toast(err.message, 'error'); }
}

// === LOGS ===

async function renderLogs() {
  const el = $('page-logs');
  el.innerHTML = `
    ${pageHeader('scroll-text', 'Логи сервера', 'Просмотр логов шардов Master и Caves')}
    <div class="toolbar">
      <select id="log-shard" class="select-styled">
        <option value="Master">Master</option>
        <option value="Caves">Caves</option>
      </select>
      <button class="btn btn-primary btn-sm" onclick="loadLogs()">
        <i data-lucide="refresh-cw"></i> Обновить
      </button>
      <button class="btn btn-outline btn-sm" onclick="toggleAutoRefresh()" id="auto-refresh-btn">
        <i data-lucide="timer"></i> Автообновление
      </button>
      <label class="toolbar-label">
        Строк:
        <input type="number" class="input input--narrow" id="log-lines" value="100" min="10" max="5000">
      </label>
    </div>
    <div id="log-meta" class="log-meta text-muted-sm"></div>
    <div id="log-container"><div class="loading-center"><span class="spinner"></span></div></div>
  `;
  refreshIcons();
  if (pendingLogShard && $('log-shard')) {
    $('log-shard').value = pendingLogShard;
    pendingLogShard = null;
  }
  await loadLogs();
}

function toggleAutoRefresh() {
  autoRefresh = !autoRefresh;
  const btn = $('auto-refresh-btn');
  btn.innerHTML = autoRefresh
    ? '<i data-lucide="pause"></i> Остановить'
    : '<i data-lucide="timer"></i> Автообновление';
  refreshIcons();
  if (autoRefresh) {
    if (logPollInterval) clearInterval(logPollInterval);
    logPollInterval = setInterval(loadLogs, 3000);
  } else {
    if (logPollInterval) { clearInterval(logPollInterval); logPollInterval = null; }
  }
}

async function loadLogs() {
  const shardEl = $('log-shard');
  const linesEl = $('log-lines');
  if (!shardEl || !linesEl) return;
  const shard = shardEl.value;
  const lines = linesEl.value;
  const meta = $('log-meta');
  const container = $('log-container');
  try {
    const data = await API.get(`/api/server/logs/${shard}?lines=${lines}`);
    const logLines = data.lines || [];
    if (meta) {
      const src = data.path ? `Источник: <code>${escHtml(data.path)}</code>` : '';
      const msg = data.message ? escHtml(data.message) : '';
      const alt = (data.sources || []).length > 1
        ? ` · Доступно файлов: ${data.sources.length}`
        : '';
      meta.innerHTML = [msg, src].filter(Boolean).join('<br>') + alt;
    }
    if (!logLines.length) {
      container.innerHTML = `
        <div class="log-empty">
          <i data-lucide="file-x"></i>
          <p>${escHtml(data.message || 'Лог пуст или файл ещё не создан.')}</p>
          <p class="text-muted-sm">Запустите шард и нажмите «Обновить». Также проверьте panel_launch.log в /var/lib/dst-panel/shard-logs/</p>
        </div>
      `;
    } else {
      container.innerHTML = logLines.map(l => `<div class="log-line">${escHtml(l)}</div>`).join('');
      container.scrollTop = container.scrollHeight;
    }
    refreshIcons();
  } catch (err) {
    if (meta) meta.innerHTML = '';
    container.innerHTML = `
      <div class="log-empty">
        <i data-lucide="alert-circle"></i>
        <p>Не удалось загрузить лог</p>
      </div>
    `;
    toast(err.message, 'error');
    refreshIcons();
  }
}

// === BACKUPS ===

function backupIncludesLabel(includes) {
  if (!includes) return 'кластер';
  const parts = [];
  if (includes.cluster) parts.push('кластер');
  if (includes.mods) parts.push('моды');
  if (includes.workshop) parts.push('workshop');
  if (includes.player_records) parts.push('игроки');
  if (includes.world_library) parts.push('библиотека миров');
  return parts.join(', ') || 'кластер';
}

async function renderBackups() {
  const el = $('page-backups');
  el.innerHTML = `
    ${pageHeader('archive', 'Резервные копии', 'Перенос сервера на другую машину и восстановление')}
    <p class="text-muted-sm">
      <strong>Перенос:</strong> создайте бэкап → скачайте → на новом сервере <code>install.sh</code> и DST → загрузите → восстановите → выберите мир → запустите кластер.
      В бэкап входят: активный сейв, библиотека миров, конфиги, токен Klei, моды, workshop, история игроков.
    </p>
    <div class="btn-group mt-12">
      <button class="btn btn-success btn-sm" onclick="createBackup()" ${isAdmin() ? '' : 'disabled'}>
        <i data-lucide="plus"></i> Создать полный бэкап
      </button>
      <label class="btn btn-primary btn-sm" ${isOwner() ? '' : 'style="opacity:0.5;pointer-events:none"'}>
        <i data-lucide="upload"></i> Загрузить бэкап
        <input type="file" accept=".tar.gz,.tgz" class="hidden" id="backup-upload-input" onchange="uploadBackup(this)" ${isOwner() ? '' : 'disabled'}>
      </label>
    </div>
    <div id="backups-list"><div class="loading-center"><span class="spinner"></span></div></div>
  `;
  refreshIcons();
  await loadBackups();
}

async function loadBackups() {
  try {
    const data = await API.get('/api/backup/list');
    const backups = data.backups || [];
    if (backups.length === 0) {
      $('backups-list').innerHTML = '<div class="card"><p class="text-muted">Бэкапов пока нет. Создайте полный бэкап для переноса на другой сервер.</p></div>';
      refreshIcons();
      return;
    }
    $('backups-list').innerHTML = `
      <div class="table-wrap">
        <table>
          <tr><th>Файл</th><th>Тип</th><th>Содержимое</th><th>Размер</th><th>Создан</th><th>Действия</th></tr>
          ${backups.map(b => `
            <tr>
              <td>${escHtml(b.filename)}</td>
              <td>${b.backup_type === 'full' ? '<span class="badge ok">полный</span>' : '<span class="badge warn">старый</span>'}</td>
              <td>${escHtml(backupIncludesLabel(b.includes))}</td>
              <td>${fmtBytes(b.size_bytes)}</td>
              <td>${escHtml(b.created_at)}</td>
              <td class="table-actions">
                <a class="btn btn-outline btn-sm" href="/api/backup/download/${encodeURIComponent(b.filename)}" download>
                  <i data-lucide="download"></i>
                </a>
                <button class="btn btn-warning btn-sm" onclick="restoreBackup('${escAttr(b.filename)}')" ${isOwner() ? '' : 'disabled'} title="Восстановить на этом сервере">
                  <i data-lucide="undo-2"></i>
                </button>
                <button class="btn btn-danger btn-sm" onclick="deleteBackup('${escAttr(b.filename)}')" ${isAdmin() ? '' : 'disabled'}>
                  <i data-lucide="trash-2"></i>
                </button>
              </td>
            </tr>
          `).join('')}
        </table>
      </div>
    `;
    refreshIcons();
  } catch (err) {
    toast(err.message, 'error');
    $('backups-list').innerHTML = '<div class="card"><p class="text-muted">Не удалось загрузить список бэкапов.</p></div>';
    refreshIcons();
  }
}

async function createBackup() {
  if (!confirm(
    'Создать полный бэкап?\n\n' +
    'Перед созданием Master и Caves будут остановлены, чтобы сохранить целостность мира.'
  )) return;
  try {
    toast('Остановка шардов и создание бэкапа...', 'info');
    const data = await API.post('/api/backup/create');
    if (data.success) {
      toast(data.message || 'Бэкап создан', 'success');
      if (data.warnings?.length) toast(data.warnings.join(' '), 'warning');
      loadBackups();
    } else {
      toast('Ошибка: ' + data.error, 'error');
      if (data.hint) toast(data.hint, 'warning');
    }
  } catch (err) { toast(err.message, 'error'); }
}

async function uploadBackup(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  if (!file.name.endsWith('.tar.gz') && !file.name.endsWith('.tgz')) {
    toast('Нужен файл .tar.gz', 'error');
    return;
  }
  try {
    toast('Загрузка бэкапа...', 'info');
    const data = await API.upload('/api/backup/upload', file);
    toast(data.message || 'Бэкап загружен', 'success');
    loadBackups();
  } catch (err) { toast(err.message, 'error'); }
}

async function restoreBackup(filename) {
  if (!confirm(
    `Восстановить бэкап «${filename}»?\n\n` +
    'Текущий кластер, моды и история игроков будут заменены.\n' +
    'Шарды Master/Caves будут остановлены.'
  )) return;
  try {
    toast('Восстановление... Остановка шардов и применение бэкапа', 'info');
    const data = await API.post(`/api/backup/restore/${encodeURIComponent(filename)}`);
    if (data.success) {
      toast(data.message || 'Бэкап восстановлен', 'success');
      if (data.restored?.length) toast('Восстановлено: ' + data.restored.join(', '), 'info');
      navigateTo('server');
    } else toast('Ошибка восстановления: ' + data.error, 'error');
  } catch (err) { toast(err.message, 'error'); }
}

async function deleteBackup(filename) {
  if (!confirm(`Удалить бэкап «${filename}»?`)) return;
  try {
    await API.del(`/api/backup/${filename}`);
    toast('Бэкап удалён', 'success');
    loadBackups();
  } catch (err) { toast(err.message, 'error'); }
}

// === USERS ===

async function renderUsers() {
  const el = $('page-users');
  el.innerHTML = '<div class="loading-center"><span class="spinner"></span></div>';
  refreshIcons();
  try {
    const users = await API.get('/api/auth/users');
    el.innerHTML = `
      ${pageHeader('users', 'Пользователи', 'Управление учётными записями панели')}
      <div class="btn-group" style="margin-bottom:16px;">
        <button class="btn btn-success btn-sm" onclick="showCreateUserModal()" ${isOwner() ? '' : 'disabled'}>
          <i data-lucide="user-plus"></i> Добавить пользователя
        </button>
      </div>
      <div class="table-wrap">
        <table>
          <tr><th>ID</th><th>Логин</th><th>Роль</th><th>2FA</th><th>Действия</th></tr>
          ${users.map(u => `
            <tr>
              <td>${u.id}</td>
              <td>${escHtml(u.username)}</td>
              <td>${roleLabel(u.role)}</td>
              <td><span class="badge ${u.totp_enabled ? 'ok' : ''}">${u.totp_enabled ? 'Вкл' : 'Выкл'}</span></td>
              <td>
                <button class="btn btn-primary btn-sm" onclick="showEditUserModal(${u.id},'${escAttr(u.role)}')" ${isOwner() ? '' : 'disabled'}>
                  <i data-lucide="pencil"></i> Изменить
                </button>
                <button class="btn btn-danger btn-sm" onclick="deleteUser(${u.id})" ${isOwner() && u.username !== currentUser.username ? '' : 'disabled'}>
                  <i data-lucide="trash-2"></i> Удалить
                </button>
              </td>
            </tr>
          `).join('')}
        </table>
      </div>
    `;
    refreshIcons();
  } catch (err) {
    renderLoadError(el, err.message, 'renderUsers');
  }
}

function showCreateUserModal() {
  editingUserId = null;
  showModal('Новый пользователь', `
    <div class="modal-field"><label>Логин</label><input type="text" class="input" id="modal-username"></div>
    <div class="modal-field"><label>Пароль</label><input type="password" class="input" id="modal-password"></div>
    <div class="modal-field"><label>Роль</label>
    <select id="modal-role" class="select-styled">
      <option value="viewer">Наблюдатель</option>
      <option value="operator">Оператор</option>
      <option value="admin">Администратор</option>
      <option value="owner">Владелец</option>
    </select></div>
    <div class="btn-group">
      <button class="btn btn-success" onclick="saveUser()"><i data-lucide="check"></i> Создать</button>
      <button class="btn btn-outline" onclick="closeModal()">Отмена</button>
    </div>
  `);
}

function showEditUserModal(id, role) {
  editingUserId = id;
  showModal('Редактирование пользователя', `
    <div class="modal-field"><label>Роль</label>
    <select id="modal-role" class="select-styled">
      <option value="viewer" ${role === 'viewer' ? 'selected' : ''}>Наблюдатель</option>
      <option value="operator" ${role === 'operator' ? 'selected' : ''}>Оператор</option>
      <option value="admin" ${role === 'admin' ? 'selected' : ''}>Администратор</option>
      <option value="owner" ${role === 'owner' ? 'selected' : ''}>Владелец</option>
    </select></div>
    <div class="modal-field"><label>Новый пароль (оставьте пустым, чтобы не менять)</label>
    <input type="password" class="input" id="modal-password"></div>
    <div class="btn-group">
      <button class="btn btn-primary" onclick="saveUser()"><i data-lucide="save"></i> Сохранить</button>
      <button class="btn btn-outline" onclick="closeModal()">Отмена</button>
    </div>
  `);
}

async function saveUser() {
  const username = $('modal-username')?.value;
  const password = $('modal-password')?.value;
  const role = $('modal-role')?.value;
  try {
    if (editingUserId) {
      await API.put(`/api/auth/users/${editingUserId}`, { role, password: password || undefined });
      toast('Пользователь обновлён', 'success');
    } else {
      await API.post('/api/auth/users', { username, password, role });
      toast('Пользователь создан', 'success');
    }
    closeModal();
    renderUsers();
  } catch (err) { toast(err.message, 'error'); }
}

async function deleteUser(id) {
  if (!confirm('Удалить этого пользователя?')) return;
  try {
    await API.del(`/api/auth/users/${id}`);
    toast('Пользователь удалён', 'success');
    renderUsers();
  } catch (err) { toast(err.message, 'error'); }
}

// === AUDIT ===

async function renderAudit() {
  const el = $('page-audit');
  el.innerHTML = '<div class="loading-center"><span class="spinner"></span></div>';
  refreshIcons();
  try {
    const data = await API.get('/api/audit/logs?limit=200');
    const logs = data.logs || [];
    el.innerHTML = `
      ${pageHeader('clipboard-list', 'Журнал аудита', 'История действий пользователей панели')}
      <div class="table-wrap">
        <table>
          <tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Детали</th><th>IP</th></tr>
          ${logs.length ? logs.map(l => `
            <tr>
              <td class="table-nowrap">${escHtml(l.created_at)}</td>
              <td>${escHtml(l.username)}</td>
              <td>${escHtml(l.action)}</td>
              <td>${escHtml(l.details || '')}</td>
              <td>${escHtml(l.ip_address || '')}</td>
            </tr>
          `).join('') : '<tr><td colspan="5" class="empty-cell">Записей нет</td></tr>'}
        </table>
      </div>
    `;
    refreshIcons();
  } catch (err) {
    renderLoadError(el, err.message, 'renderAudit');
  }
}

// === SETTINGS ===

function renderSettings() {
  $('page-settings').innerHTML = `
    ${pageHeader('user-cog', 'Аккаунт', 'Настройки вашей учётной записи')}
    <div class="card">
      <div class="card-header"><i data-lucide="user"></i><h3>Профиль</h3></div>
      <p class="text-muted">Вы вошли как <strong>${escHtml(currentUser.username)}</strong> (${roleLabel(currentUser.role)})</p>
      <div class="btn-group">
        <button class="btn btn-primary btn-sm" onclick="showChangePasswordModal()">
          <i data-lucide="key-round"></i> Сменить пароль
        </button>
        <button class="btn btn-primary btn-sm" onclick="setup2FA()" ${currentUser.totp_enabled ? 'disabled' : ''}>
          <i data-lucide="shield"></i> ${currentUser.totp_enabled ? '2FA включена' : 'Включить 2FA'}
        </button>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><i data-lucide="info"></i><h3>О панели</h3></div>
      <p style="color:var(--text-muted);font-size:13px;">DST Panel v1.0.0</p>
      <p style="color:var(--text-muted);font-size:13px;">FastAPI + SQLite + Vanilla JS</p>
    </div>
  `;
  refreshIcons();
}

function showChangePasswordModal() {
  showModal('Смена пароля', `
    <div class="modal-field"><label>Текущий пароль</label><input type="password" class="input" id="modal-current-pw"></div>
    <div class="modal-field"><label>Новый пароль</label><input type="password" class="input" id="modal-new-pw"></div>
    <div class="modal-field"><label>Подтвердите новый пароль</label><input type="password" class="input" id="modal-confirm-pw"></div>
    <div class="btn-group">
      <button class="btn btn-primary" onclick="changePassword()"><i data-lucide="check"></i> Сменить</button>
      <button class="btn btn-outline" onclick="closeModal()">Отмена</button>
    </div>
  `);
}

async function changePassword() {
  const cur = $('modal-current-pw').value;
  const newPw = $('modal-new-pw').value;
  const confirm = $('modal-confirm-pw').value;
  if (newPw !== confirm) { toast('Пароли не совпадают', 'error'); return; }
  try {
    await API.post('/api/auth/password', { current_password: cur, new_password: newPw });
    toast('Пароль изменён', 'success');
    closeModal();
  } catch (err) { toast(err.message, 'error'); }
}

async function setup2FA() {
  showModal('Включение 2FA', `
    <div class="modal-field"><label>Подтвердите пароль</label><input type="password" class="input" id="modal-2fa-pw"></div>
    <div class="btn-group">
      <button class="btn btn-primary" onclick="init2FA()"><i data-lucide="arrow-right"></i> Далее</button>
      <button class="btn btn-outline" onclick="closeModal()">Отмена</button>
    </div>
  `);
}

async function init2FA() {
  const pw = $('modal-2fa-pw').value;
  try {
    const data = await API.post('/api/auth/2fa/enable', { password: pw });
    totpSecret = data.secret;
    showModal('Сканируйте QR-код', `
      <p class="text-muted">Отсканируйте QR-код в приложении-аутентификаторе (Google Authenticator, Authy и т.д.)</p>
      <div class="qr-wrap">
        <img src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(data.uri)}" alt="QR Code">
      </div>
      <p class="text-muted-sm">Или введите вручную: <code class="code-inline" style="display:inline;padding:2px 6px;margin:0;">${escHtml(data.secret)}</code></p>
      <div class="modal-field">
        <label>Введите 6-значный код для подтверждения</label>
        <input type="text" class="input" id="modal-2fa-code" placeholder="000000" maxlength="6">
      </div>
      <div class="btn-group">
        <button class="btn btn-success" onclick="verify2FA()"><i data-lucide="shield-check"></i> Подтвердить</button>
        <button class="btn btn-outline" onclick="closeModal()">Отмена</button>
      </div>
    `);
  } catch (err) { toast(err.message, 'error'); }
}

async function verify2FA() {
  const code = $('modal-2fa-code').value;
  if (code.length !== 6) { toast('Введите 6-значный код', 'error'); return; }
  try {
    await API.post('/api/auth/2fa/verify', { code });
    toast('2FA успешно включена', 'success');
    closeModal();
    currentUser.totp_enabled = true;
  } catch (err) { toast(err.message, 'error'); }
}

// === MODAL ===

function showModal(title, content) {
  const existing = document.querySelector('.modal-overlay');
  if (existing) existing.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `<div class="modal"><h3>${escHtml(title)}</h3>${content}</div>`;
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  refreshIcons();
}

function closeModal() {
  document.querySelector('.modal-overlay')?.remove();
}
