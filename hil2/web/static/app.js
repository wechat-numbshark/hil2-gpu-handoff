/* HIL2.0 集群状态大盘 · 前端
 *
 * 无构建步骤、无第三方依赖：直接由协调服务静态托管，内网离线也能跑。
 * 拉取失败时保留上一次数据并显式标注「不代表当前真实状态」，
 * 与后端 fail-closed 的原则一致——宁可说不知道，也不假装正常。
 */
(() => {
  'use strict';

  const CONFIG = window.HIL2_CONFIG || { refreshSeconds: 60 };
  const REFRESH_MS = Math.max(10, CONFIG.refreshSeconds) * 1000;
  const RING_LENGTH = 2 * Math.PI * 15.5;

  const GROUPS = [
    { key: 'unknown',        label: '采集异常', icon: 'i-unknown' },
    { key: 'suspected_idle', label: '待确认',   icon: 'i-suspected' },
    { key: 'busy',           label: '使用中',   icon: 'i-busy' },
    { key: 'reserved',       label: '已保留',   icon: 'i-reserved' },
    { key: 'idle',           label: '空闲',     icon: 'i-idle' },
    { key: 'no_data',        label: '无数据',   icon: 'i-nodata' }
  ];
  const GROUP_BY_KEY = Object.fromEntries(GROUPS.map((g) => [g.key, g]));
  const ATTENTION = new Set(['unknown', 'suspected_idle']);

  const ACTION_LABEL = {
    audit_only: '仅审计',
    ask_current_user: '发确认卡片',
    notify_next_available: '通知下一位',
    alert_admin: '告警管理员',
    remind_current_user: '提醒占用人',
    extend_reservation: '延长保留',
    release_and_notify_next: '释放并通知',
    reject_stale_or_unauthorized: '拒绝无效点击',
    card_callback: '卡片回调',
    booking_created: '新建预约',
    booking_cancelled: '取消预约',
    booking_reminder: '预约提醒'
  };
  // 动作流水按语义着色，而不是「重要 / 不重要」两档：
  // 告警走异常色，问人走待确认色，交接走可用色，保留走保留色，其余保持安静。
  const ACTION_TONE = {
    alert_admin: 'alert',
    ask_current_user: 'ask',
    remind_current_user: 'ask',
    notify_next_available: 'handoff',
    release_and_notify_next: 'handoff',
    extend_reservation: 'hold',
    booking_created: 'hold',
    booking_reminder: 'ask'
  };

  const INCIDENT_LABEL = {
    collection_failed: '采集异常',
    delivery_failed: '飞书投递失败',
    reservation_source_failed: '预约数据源不可用',
    unattended_confirmation: '确认卡片无人回复',
    unreserved_usage: '未预约使用',
    malformed_event: '事件格式错误'
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    grid: $('grid'), empty: $('empty'), tally: $('tally'), fleetBar: $('fleet-bar'),
    observed: $('meta-observed'), generated: $('meta-generated'),
    offline: $('offline'), offlineDetail: $('offline-detail'), offlineRetry: $('offline-retry'),
    incidents: $('incidents'), incidentsCount: $('incidents-count'), incidentsEmpty: $('incidents-empty'),
    feed: $('feed'), feedEmpty: $('feed-empty'),
    refreshRing: $('refresh-ring'), refreshCount: $('refresh-count'), refreshNow: $('refresh-now'),
    themeToggle: $('theme-toggle'),
    filterNote: $('filter-note'), filterLabel: $('filter-label'), filterClear: $('filter-clear')
  };

  let latest = null;
  let filter = null;
  let previousStates = new Map();
  let deadline = 0;
  let timer = null;
  let inflight = false;

  /* ---------------- 工具 ---------------- */
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));

  const icon = (name, cls = 'icon') => `<svg class="${cls}" aria-hidden="true"><use href="#${name}"/></svg>`;

  function relativeTime(iso) {
    if (!iso) return '—';
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return '—';
    const seconds = Math.round((Date.now() - then) / 1000);
    if (seconds < 0) return '刚刚';
    if (seconds < 60) return `${seconds} 秒前`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.round(hours / 24)} 天前`;
  }

  function clockTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function countdown(iso) {
    if (!iso) return null;
    const minutes = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
    if (Number.isNaN(minutes) || minutes <= 0) return null;
    return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes} 分钟`;
  }

  const person = (name, openId) => name || (openId ? `${openId.slice(0, 10)}…` : '');

  /* ---------------- 渲染 ---------------- */
  function renderFleet(data) {
    const counters = data.counters || {};
    const total = GROUPS.reduce((sum, g) => sum + (counters[g.key] || 0), 0) || 1;

    el.fleetBar.innerHTML = GROUPS
      .filter((g) => counters[g.key] > 0)
      .map((g) => {
        const active = filter === g.key ? ' is-active' : '';
        const count = counters[g.key];
        return `<span class="fleet__seg${active}" data-state="${g.key}"
                  style="flex:${count}" title="${g.label} ${count} 台"></span>`;
      })
      .join('') || `<span class="fleet__seg" data-state="no_data" style="flex:1"></span>`;
    el.fleetBar.dataset.dim = filter ? '1' : '0';
    el.fleetBar.setAttribute(
      'aria-label',
      GROUPS.filter((g) => counters[g.key] > 0)
        .map((g) => `${g.label} ${counters[g.key]} 台`).join('，') || '暂无数据'
    );

    const extras = [
      { key: 'open_incidents', label: '未恢复事件', icon: 'i-alert', state: 'unknown' },
      { key: 'pending_cards', label: '待回复卡片', icon: 'i-bell', state: 'suspected_idle' }
    ];

    el.tally.innerHTML = GROUPS.map((g) => {
      const count = counters[g.key] || 0;
      return `<button type="button" class="tally__item" data-state="${g.key}"
                data-zero="${count ? 0 : 1}" aria-pressed="${filter === g.key}"
                ${count ? '' : 'disabled'}>
                <span class="tally__n">${count}</span>
                <span class="tally__label">${icon(g.icon)}${g.label}</span>
              </button>`;
    }).join('') + extras.map((x) => {
      const count = counters[x.key] || 0;
      return `<div class="tally__item" data-state="${x.state}" data-zero="${count ? 0 : 1}"
                style="cursor:default">
                <span class="tally__n">${count}</span>
                <span class="tally__label">${icon(x.icon)}${x.label}</span>
              </div>`;
    }).join('');

    el.tally.querySelectorAll('button[data-state]').forEach((button) => {
      button.addEventListener('click', () => setFilter(
        filter === button.dataset.state ? null : button.dataset.state
      ));
    });
    // 单位是「台」：total 已用于占比计算，这里不再重复展示避免读者算两次。
    void total;
  }

  function containerFacts(item) {
    const ROLE_LABEL = { sim: 'sim', proxy: 'proxy', tester: 'tester' };
    return (item.containers || []).map((role) => {
      if (!role.matched) {
        return `<div class="fact">
          <div class="fact__label">${icon('i-container')}${ROLE_LABEL[role.role]}</div>
          <div class="fact__value" data-tone="unknown">未匹配</div>
        </div>`;
      }
      const dots = Array.from({ length: Math.min(role.total, 6) }, (_, i) =>
        `<i class="${i < role.running ? 'on' : ''}"></i>`).join('');
      return `<div class="fact">
        <div class="fact__label">${icon('i-container')}${ROLE_LABEL[role.role]}</div>
        <div class="fact__value" data-tone="${role.running ? 'on' : 'off'}">${role.running}/${role.total}<span class="dots">${dots}</span></div>
      </div>`;
    }).join('');
  }

  function activityFacts(item) {
    const tone = (value) => (value === null || value === undefined ? 'unknown' : (value ? 'on' : 'off'));
    const text = (value, yes, no) => (value === null || value === undefined ? '未知' : (value ? yes : no));
    return `<div class="fact">
        <div class="fact__label">${icon('i-flask')}测试进程</div>
        <div class="fact__value" data-tone="${tone(item.pytest_active)}">${text(item.pytest_active, '运行中', '无')}</div>
      </div>
      <div class="fact">
        <div class="fact__label">${icon('i-disk')}数据写入</div>
        <div class="fact__value" data-tone="${tone(item.recent_data)}">${text(item.recent_data, '近期有', '无')}</div>
      </div>
      <div class="fact" title="${esc((item.login_users || []).join(' / ') || item.login_note || '没有登录用户')}">
        <div class="fact__label">${icon('i-user')}登录会话 · 辅助</div>
        <div class="fact__value fact__value--clip" data-tone="${tone((item.login_session_count ?? 0) > 0)}">${
          item.login_session_count == null
            ? '未知'
            : `${item.login_session_count} 个${item.login_users?.length ? ` · ${esc(item.login_users.join(' / '))}` : ''}`
        }</div>
      </div>`;
  }

  function gpuBlock(item) {
    if (!item.gpus || !item.gpus.length) {
      if (!item.gpu_note) return '';
      return `<div class="gpu"><div class="gpu__head">${icon('i-chip')}GPU 参考 · 不参与判定</div>
        <div class="fact__value" data-tone="off" style="font-size:.76rem">${esc(item.gpu_note)}</div></div>`;
    }
    const bars = item.gpus.slice(0, 8).map((gpu) => {
      const pct = Math.max(3, Math.min(100, gpu.utilization ?? 0));
      const title = `GPU${gpu.index}：${gpu.utilization ?? '?'}%${gpu.memory_used_mb != null ? ` · ${gpu.memory_used_mb} MiB` : ''}`;
      return `<div class="gpu__bar" title="${esc(title)}"><span style="height:${pct}%"></span></div>`;
    }).join('');
    return `<div class="gpu">
      <div class="gpu__head">${icon('i-chip')}GPU 参考 · 不参与判定</div>
      <div class="gpu__bars">${bars}</div>
    </div>`;
  }

  function reservationRow(item) {
    const res = item.reservation || {};
    const current = person(res.current_name, res.current_open_id);
    const next = person(res.next_name, res.next_open_id);
    const hold = countdown(res.reserved_until);

    const parts = [];
    parts.push(current
      ? `<span class="res__who">${icon('i-user')}${esc(current)}</span>`
      : `<span class="res__none">当前无人占用</span>`);
    if (next) {
      parts.push(`<span class="res__arrow">${icon('i-handoff')}</span>`);
      parts.push(`<span class="res__who">${icon('i-user')}${esc(next)}</span>`);
    }
    if (res.handoff_at && !hold) {
      parts.push(`<span class="tag${res.handoff_due ? ' tag--warn' : ''}">交接 ${clockTime(res.handoff_at)}</span>`);
    }
    if (hold) parts.push(`<span class="res__hold num">保留还剩 ${hold}</span>`);
    return `<div class="res">${parts.join('')}</div>`;
  }

  function awaitingRow(item) {
    if (!item.pending_card) return '';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(item.pending_card.created_at).getTime()) / 60000));
    const chips = [];
    if (item.pending_card.reminded) chips.push('<span class="tag">已提醒</span>');
    if (item.pending_card.escalated) chips.push('<span class="tag tag--warn">已升级管理员</span>');
    return `<div class="awaiting">${icon('i-bell')}
      <span>已向当前占用人发出确认卡片，<b>${minutes}</b> 分钟未回复</span>
      ${chips.join('')}
      <span style="margin-left:auto;opacity:.85">不会自动释放</span>
    </div>`;
  }

  function ageMeter(item) {
    if (item.age_seconds === null || item.age_seconds === undefined) return '';
    const limit = (latest?.stale_after_minutes || 20) * 60;
    const ratio = Math.max(.04, Math.min(1, item.age_seconds / limit));
    return `<span class="age" data-stale="${item.stale ? 1 : 0}">
      <span class="age__track"><span class="age__fill" style="transform:scaleX(${ratio.toFixed(3)})"></span></span>
      <span class="num">采集于 ${relativeTime(item.observed_at)}</span>
    </span>`;
  }

  function cardHtml(item) {
    const group = GROUP_BY_KEY[item.group] || GROUP_BY_KEY.no_data;
    const attention = ATTENTION.has(item.group);
    const changed = previousStates.has(item.cluster) && previousStates.get(item.cluster) !== item.group;

    const classes = ['card'];
    if (attention) classes.push('card--attention');
    if (item.group === 'no_data') classes.push('card--muted');
    if (changed) classes.push('card--settle');

    const tags = [];
    if (!item.managed) tags.push('<span class="tag">未纳管</span>');
    if (item.stale) tags.push('<span class="tag tag--warn">数据可能过期</span>');
    if (item.incidents?.length) {
      tags.push(`<span class="tag tag--warn">${item.incidents.length} 个未恢复事件</span>`);
    }
    if (item.incidents?.some((incident) => incident.kind === 'unreserved_usage')) {
      tags.push('<span class="tag tag--danger">未预约使用</span>');
    }

    const reasons = (item.reasons || []).length
      ? `<ul class="card__reasons">${item.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`
      : '';
    const warnings = (item.warnings || []).length
      ? `<ul class="card__warnings">${item.warnings.map((w) => `<li>${icon('i-alert')}<span>${esc(w)}</span></li>`).join('')}</ul>`
      : '';

    const suspected = item.group === 'suspected_idle' && item.suspected_idle_minutes
      ? `<span class="num">已持续 ${item.suspected_idle_minutes} 分钟</span>` : '';

    return `<article class="${classes.join(' ')}" data-state="${item.group}" data-cluster="${esc(item.cluster)}">
      <div class="card__head">
        <div class="card__no">${esc(item.cluster)}</div>
        <div class="card__headings">
          <div class="card__title">
            <span class="pill">${icon(group.icon)}${esc(item.state_label || group.label)}</span>
            ${tags.join('')}
          </div>
          <div class="card__meta">
            ${item.hostname ? `${icon('i-server')}<span class="num">${esc(item.hostname)}</span>` : ''}
            ${item.primary_ip ? `<span class="num">${esc(item.primary_ip)}</span>` : ''}
            ${suspected}
          </div>
        </div>
      </div>
      ${reasons}
      ${warnings}
      <div class="facts">${containerFacts(item)}</div>
      <div class="facts">${activityFacts(item)}</div>
      ${gpuBlock(item)}
      ${reservationRow(item)}
      ${awaitingRow(item)}
      <div class="card__foot">
        ${ageMeter(item)}
        ${item.source ? `<span aria-hidden="true">·</span><span>${item.source === 'direct' ? '直连上报' : '监控群转发'}</span>` : ''}
        ${item.data_dir ? `<span aria-hidden="true">·</span><span class="num">${esc(item.data_dir)}</span>` : ''}
        ${item.agent_version ? `<span aria-hidden="true">·</span><span class="num">agent ${esc(item.agent_version)}</span>` : ''}
      </div>
    </article>`;
  }

  function renderGrid(data) {
    const items = filter ? data.clusters.filter((c) => c.group === filter) : data.clusters;
    el.grid.setAttribute('aria-busy', 'false');

    if (!items.length) {
      el.grid.innerHTML = '';
      el.empty.hidden = false;
      el.empty.textContent = filter
        ? `没有处于「${GROUP_BY_KEY[filter].label}」的集群。`
        : '还没有收到任何采集事件。确认集群侧 hil-agent 已启动，并配置了 HIL_COORDINATOR_URL 或 FEISHU_WEBHOOK。';
      return;
    }
    el.empty.hidden = true;
    el.grid.innerHTML = items.map(cardHtml).join('');
    data.clusters.forEach((c) => previousStates.set(c.cluster, c.group));
  }

  function renderLedger(data) {
    const incidents = data.incidents || [];
    el.incidentsCount.textContent = incidents.length;
    el.incidentsCount.dataset.hot = incidents.length ? '1' : '0';
    el.incidentsEmpty.hidden = incidents.length > 0;
    el.incidents.innerHTML = incidents.map((incident) => `<li>
      <span class="incidents__cluster">${esc(incident.cluster)}</span>
      <span class="incidents__detail"><b>${esc(INCIDENT_LABEL[incident.kind] || incident.kind)}</b>
        ${incident.detail ? ` · ${esc(incident.detail)}` : ''}</span>
      <span class="incidents__when">${relativeTime(incident.opened_at)}</span>
    </li>`).join('');

    const feed = (data.notifications || []).filter((n) => n.kind !== 'audit_only').slice(0, 12);
    el.feedEmpty.hidden = feed.length > 0;
    el.feed.innerHTML = feed.map((entry) => `<li>
      <span class="feed__kind"${ACTION_TONE[entry.kind] ? ` data-tone="${ACTION_TONE[entry.kind]}"` : ''}>${esc(ACTION_LABEL[entry.kind] || entry.kind)}</span>
      <span class="feed__note"><b class="num">${esc(entry.cluster)}</b> ${esc(entry.note || '')}${entry.delivered ? '' : ' · 投递失败'}</span>
      <span class="feed__when">${relativeTime(entry.created_at)}</span>
    </li>`).join('');
  }

  function render(data) {
    latest = data;
    el.observed.textContent = `${clockTime(data.last_observed_at)}（${relativeTime(data.last_observed_at)}）`;
    el.generated.textContent = clockTime(data.generated_at);
    renderFleet(data);
    renderGrid(data);
    renderLedger(data);
  }

  /* ---------------- 筛选 ---------------- */
  function setFilter(next) {
    filter = next;
    el.filterNote.hidden = !next;
    if (next) el.filterLabel.textContent = GROUP_BY_KEY[next].label;
    if (latest) { renderFleet(latest); renderGrid(latest); }
  }

  /* ---------------- 轮询 ---------------- */
  async function load() {
    if (inflight) return;
    inflight = true;
    try {
      const response = await (window.HIL2Auth ? window.HIL2Auth.fetchAuthed : fetch)(
        '/api/dashboard', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
      el.offline.hidden = true;
    } catch (error) {
      // 401 是「需要登录」，不是「服务挂了」——说错会让人白排查半天。
      const needsLogin = /HTTP 401/.test(error.message);
      el.offline.hidden = false;
      el.offline.querySelector('span').innerHTML = needsLogin
        ? '<b>需要登录才能查看。</b>点右上角「飞书登录」。'
        : '<b>与协调服务失去连接。</b>下方仍是最后一次成功读取的数据，<b>不代表当前真实状态</b>。';
      el.offlineDetail.textContent = needsLogin
        ? '' : `${new Date().toLocaleTimeString('zh-CN')} · ${error.message}`;
      el.grid.setAttribute('aria-busy', 'false');
    } finally {
      inflight = false;
      deadline = Date.now() + REFRESH_MS;
    }
  }

  function tick() {
    const remain = Math.max(0, deadline - Date.now());
    const ratio = 1 - remain / REFRESH_MS;
    el.refreshRing.style.strokeDashoffset = String(RING_LENGTH * (1 - ratio));
    el.refreshCount.textContent = ` ${Math.ceil(remain / 1000)}s`;
    // 相对时间会随时间漂移，卡片上的「x 分钟前」每秒复算一次成本太高，
    // 因此只在整分钟边界重绘一次。
    if (latest && Math.ceil(remain / 1000) % 30 === 0) {
      el.observed.textContent = `${clockTime(latest.last_observed_at)}（${relativeTime(latest.last_observed_at)}）`;
    }
    if (remain <= 0) load();
  }

  /* ---------------- 主题 ---------------- */
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('hil2-theme', theme); } catch { /* 隐私模式下忽略 */ }
  }

  function initTheme() {
    let stored = null;
    try { stored = localStorage.getItem('hil2-theme'); } catch { /* 忽略 */ }
    applyTheme(stored || 'light');
  }

  /* ---------------- 启动 ---------------- */
  el.refreshNow.addEventListener('click', () => { deadline = 0; load(); });
  el.offlineRetry.addEventListener('click', () => { deadline = 0; load(); });
  el.filterClear.addEventListener('click', () => setFilter(null));
  el.themeToggle.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
  document.addEventListener('visibilitychange', () => {
    // 墙面屏会被切走，回来时立刻刷新，避免盯着一屏过期数据。
    if (!document.hidden) { deadline = 0; load(); }
  });

  // 登录态：显示用户名 / 登录按钮 / 后台入口
  const auth = window.HIL2Auth;
  if (auth) {
    const who = $('who');
    const whoLabel = $('who-label');
    const whoAvatar = $('who-avatar');
    const loginBtn = $('login-btn');
    const adminLink = $('admin-link');

    auth.onChange((session) => {
      const user = session.user;
      who.hidden = !user;
      loginBtn.hidden = !!user || !session.sso_enabled;
      adminLink.hidden = !session.is_admin;
      if (user) {
        whoLabel.textContent = user.name || user.open_id.slice(0, 10);
        if (user.avatar_url) { whoAvatar.src = user.avatar_url; whoAvatar.hidden = false; }
        else { whoAvatar.hidden = true; }
      }
    });
    loginBtn?.addEventListener('click', async () => {
      try { await auth.login(); } catch (error) { window.alert(error.message); }
    });
    $('who-logout')?.addEventListener('click', () => auth.logout());
    auth.refresh();
  }

  initTheme();
  el.refreshRing.style.strokeDasharray = String(RING_LENGTH);
  load();
  timer = setInterval(tick, 1000);
  window.addEventListener('beforeunload', () => clearInterval(timer));
})();

/* ============================================================
 * 预约看板
 * 横轴 48 个半小时槽位，纵轴集群。空白处按下拖动即可选时段。
 * 所有写操作都走 REST，冲突由后端判定（前端只做即时反馈）。
 * ============================================================ */
(() => {
  'use strict';

  const SLOT_MIN = 30;
  const DAY_START_HOUR = Number(window.HIL2_CONFIG?.bookingDayStartHour ?? 9);
  const DAY_END_HOUR = Number(window.HIL2_CONFIG?.bookingDayEndHour ?? 24);
  const SLOTS = (DAY_END_HOUR - DAY_START_HOUR) * 60 / SLOT_MIN;
  const IDENTITY_KEY = 'hil2-identity';

  const $ = (id) => document.getElementById(id);
  const el = {
    board: $('board'), hours: $('board-hours'), rows: $('board-rows'), nowline: $('board-nowline'),
    prev: $('day-prev'), next: $('day-next'), today: $('day-today'), label: $('day-label'),
    who: $('who-name'),
    sheet: $('book-sheet'), sheetCluster: $('sheet-cluster'), sheetRange: $('sheet-range'),
    sheetDuration: $('sheet-duration'), sheetNote: $('sheet-note'), sheetError: $('sheet-error'),
    sheetConfirm: $('sheet-confirm'), sheetCancel: $('sheet-cancel'), sheetClose: $('sheet-close')
  };
  if (!el.board) return;

  let dayOffset = 0;
  let data = null;
  let scrolledToNow = false;
  let drag = null;      // { cluster, from, to }
  let pending = null;   // 待确认的预约

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const icon = (name, cls = 'icon') => `<svg class="${cls}" aria-hidden="true"><use href="#${name}"/></svg>`;

  // 身份统一由 auth 模块提供：登录了就用真人，没登录且免登模式才用自报姓名
  const auth = window.HIL2Auth;
  const identity = {
    get name() { return auth?.user?.name || auth?.localName || ''; },
    get openId() { return auth?.user?.open_id || ''; },
    get ready() { return !!this.openId; },
    get needsLogin() { return !!auth?.session?.sso_enabled && !auth?.user; }
  };

  /* ---------------- 时间 ---------------- */
  function dayStart(offset = dayOffset) {
    const base = new Date();
    base.setHours(DAY_START_HOUR, 0, 0, 0);
    base.setDate(base.getDate() + offset);
    return base;
  }

  const slotToDate = (index, offset = dayOffset) =>
    new Date(dayStart(offset).getTime() + index * SLOT_MIN * 60000);

  function slotIndex(iso, offset = dayOffset) {
    const start = dayStart(offset).getTime();
    return Math.round((new Date(iso).getTime() - start) / (SLOT_MIN * 60000));
  }

  const hhmm = (date) =>
    `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;

  function dayLabel() {
    if (dayOffset === 0) return '今天';
    if (dayOffset === 1) return '明天';
    if (dayOffset === -1) return '昨天';
    const d = dayStart();
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  /* ---------------- 渲染 ---------------- */
  function renderHours() {
    let html = '<div></div>';
    for (let h = DAY_START_HOUR; h < DAY_END_HOUR; h += 1) {
      html += `<div class="board__hour">${String(h).padStart(2, '0')}</div>`;
    }
    el.hours.innerHTML = html;
  }

  function barsFor(cluster) {
    const now = Date.now();
    return (data.bookings || [])
      .filter((b) => b.cluster === cluster && b.status !== 'cancelled')
      .map((b) => {
        const from = Math.max(0, slotIndex(b.start_at));
        const to = Math.min(SLOTS, slotIndex(b.effective_end));
        if (to <= 0 || from >= SLOTS || to <= from) return '';

        const mine = identity.openId && b.open_id === identity.openId;
        const past = new Date(b.effective_end).getTime() < now;
        const classes = ['bar'];
        if (b.status === 'active') classes.push('bar--active');
        else if (mine) classes.push('bar--mine');
        if (past || b.status === 'done' || b.status === 'released') classes.push('bar--past');

        const left = (from / SLOTS) * 100;
        const width = ((to - from) / SLOTS) * 100;
        const who = b.name || b.open_id.slice(0, 8);
        const title = `${who}　${b.local_start}~${b.local_end}　${b.status_label}`;
        const cancellable = b.can_cancel && !past && b.status !== 'released';
        const cancelRole = mine ? '自己的' : '他人的（管理员）';

        return `<div class="${classes.join(' ')}" style="left:${left}%;width:${width}%"
                     title="${esc(title)}" data-booking="${esc(b.id)}">
                  <span class="bar__who">${esc(who)}</span>
                  ${cancellable ? `<button type="button" class="bar__drop" data-cancel="${esc(b.id)}"
                     aria-label="取消 ${esc(who)} 的预约" title="取消${cancelRole}预约">${icon('i-x')}</button>` : ''}
                </div>`;
      })
      .join('');
  }

  function renderRows() {
    const now = Date.now();
    el.rows.innerHTML = (data.clusters || []).map((cluster) => {
      let slots = '';
      for (let i = 0; i < SLOTS; i += 1) {
        const past = slotToDate(i + 1).getTime() <= now;
        slots += `<div class="slot${past ? ' slot--past' : ''}"
                       data-cluster="${esc(cluster)}" data-slot="${i}"></div>`;
      }
      return `<div class="board__row">
                <div class="board__label">${esc(cluster)}<small>集群</small></div>
                <div class="board__track" data-cluster="${esc(cluster)}">${slots}${barsFor(cluster)}</div>
              </div>`;
    }).join('');
  }

  function renderNowLine() {
    if (dayOffset !== 0) { el.nowline.hidden = true; return; }
    const now = new Date();
    const elapsed = now.getHours() * 60 + now.getMinutes() - DAY_START_HOUR * 60;
    const span = (DAY_END_HOUR - DAY_START_HOUR) * 60;
    if (elapsed < 0 || elapsed > span) { el.nowline.hidden = true; return; }
    const ratio = elapsed / span;
    const track = el.rows.querySelector('.board__track');
    if (!track) { el.nowline.hidden = true; return; }
    const boardRect = el.board.getBoundingClientRect();
    const trackRect = track.getBoundingClientRect();
    el.nowline.hidden = false;
    el.nowline.style.left =
      `${trackRect.left - boardRect.left + el.board.scrollLeft + trackRect.width * ratio}px`;
  }

  function scrollToCurrentTime() {
    if (scrolledToNow || dayOffset !== 0 || el.board.scrollWidth <= el.board.clientWidth) return;
    const now = new Date();
    const elapsed = now.getHours() * 60 + now.getMinutes() - DAY_START_HOUR * 60;
    const span = (DAY_END_HOUR - DAY_START_HOUR) * 60;
    if (elapsed < 0 || elapsed > span) return;
    const ratio = elapsed / span;
    const track = el.rows.querySelector('.board__track');
    if (!track) return;
    const maxScroll = el.board.scrollWidth - el.board.clientWidth;
    const target = track.offsetLeft + track.scrollWidth * ratio - el.board.clientWidth * .35;
    el.board.scrollLeft = Math.max(0, Math.min(maxScroll, target));
    scrolledToNow = true;
    renderNowLine();
  }

  function render() {
    if (!data) return;
    el.label.textContent = dayLabel();
    renderRows();
    renderNowLine();
    requestAnimationFrame(scrollToCurrentTime);
  }

  /* ---------------- 拖选 ---------------- */
  function paintSelection() {
    el.rows.querySelectorAll('.slot--sel').forEach((n) => n.classList.remove('slot--sel'));
    if (!drag) return;
    const [lo, hi] = [Math.min(drag.from, drag.to), Math.max(drag.from, drag.to)];
    for (let i = lo; i <= hi; i += 1) {
      el.rows.querySelector(`.slot[data-cluster="${drag.cluster}"][data-slot="${i}"]`)
        ?.classList.add('slot--sel');
    }
  }

  el.rows.addEventListener('pointerdown', (event) => {
    const cancelBtn = event.target.closest('[data-cancel]');
    if (cancelBtn) { cancelBooking(cancelBtn.dataset.cancel); return; }

    const slot = event.target.closest('.slot');
    if (!slot || slot.classList.contains('slot--past')) return;
    // 已被占用的格子不参与拖选：让用户明确看到冲突，而不是选完再被拒
    if (event.target.closest('.bar')) return;

    drag = {
      cluster: slot.dataset.cluster,
      from: Number(slot.dataset.slot),
      to: Number(slot.dataset.slot)
    };
    el.rows.setPointerCapture(event.pointerId);
    paintSelection();
  });

  el.rows.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const slot = document.elementFromPoint(event.clientX, event.clientY)?.closest('.slot');
    if (!slot || slot.dataset.cluster !== drag.cluster) return;
    drag.to = Number(slot.dataset.slot);
    paintSelection();
  });

  el.rows.addEventListener('pointerup', () => {
    if (!drag) return;
    const [lo, hi] = [Math.min(drag.from, drag.to), Math.max(drag.from, drag.to)];
    openSheet(drag.cluster, lo, hi + 1);
    drag = null;
    paintSelection();
  });

  el.rows.addEventListener('pointercancel', () => { drag = null; paintSelection(); });

  /* ---------------- 确认对话框 ---------------- */
  function openSheet(cluster, fromSlot, toSlot) {
    const start = slotToDate(fromSlot);
    const end = slotToDate(toSlot);
    pending = { cluster, start, end };

    el.sheetCluster.textContent = cluster;
    el.sheetRange.textContent = `${dayLabel()} ${hhmm(start)}~${hhmm(end)}`;
    const minutes = (toSlot - fromSlot) * SLOT_MIN;
    el.sheetDuration.textContent =
      minutes >= 60 ? `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分` : ''}` : `${minutes} 分钟`;
    el.sheetNote.value = '';
    el.sheetError.hidden = true;

    if (!identity.ready) {
      el.sheetError.hidden = false;
      el.sheetError.textContent = identity.needsLogin
        ? '先用飞书登录，才能预约。'
        : '先在上方填写你的姓名，才能预约。';
    }
    el.sheet.showModal();
  }

  function closeSheet() {
    el.sheet.close();
    pending = null;
  }

  el.sheetCancel.addEventListener('click', closeSheet);
  el.sheetClose.addEventListener('click', closeSheet);
  el.sheet.addEventListener('cancel', (e) => { e.preventDefault(); closeSheet(); });

  el.sheetConfirm.addEventListener('click', async () => {
    if (!pending) return;
    if (!identity.ready) {
      el.sheetError.hidden = false;
      el.sheetError.textContent = identity.needsLogin
        ? '先用飞书登录，才能预约。'
        : '先在上方填写你的姓名，才能预约。';
      return;
    }
    const name = identity.name.trim();
    el.sheetConfirm.disabled = true;
    try {
      const response = await (auth ? auth.fetchAuthed : fetch)('/api/bookings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          cluster: pending.cluster,
          start_at: pending.start.toISOString(),
          end_at: pending.end.toISOString(),
          open_id: identity.openId,
          name,
          note: el.sheetNote.value.trim()
        })
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      closeSheet();
      await load();
    } catch (error) {
      el.sheetError.hidden = false;
      el.sheetError.textContent = error.message;
    } finally {
      el.sheetConfirm.disabled = false;
    }
  });

  async function cancelBooking(bookingId) {
    const booking = (data.bookings || []).find((b) => b.id === bookingId);
    const label = booking ? `${booking.cluster} 集群 ${booking.local_start}~${booking.local_end}` : '这条预约';
    if (!window.confirm(`确认取消 ${label} 的预约？\n只会释放排期，不会动机器。`)) return;
    try {
      const response = await (auth ? auth.fetchAuthed : fetch)(`/api/bookings/${encodeURIComponent(bookingId)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ open_id: identity.openId, name: identity.name })
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      await load();
    } catch (error) {
      window.alert(`取消失败：${error.message}`);
    }
  }

  /* ---------------- 数据 ---------------- */
  async function load() {
    try {
      const response = await (auth ? auth.fetchAuthed : fetch)(
        `/api/bookings?days=${Math.max(1, dayOffset + 2)}&open_id=${encodeURIComponent(identity.openId)}`,
        { headers: { Accept: 'application/json' } }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      render();
    } catch (error) {
      el.rows.innerHTML =
        `<p class="panel__empty">预约数据读取失败：${esc(error.message)}</p>`;
    }
  }

  /* ---------------- 交互 ---------------- */
  el.prev.addEventListener('click', () => { dayOffset -= 1; load(); });
  el.next.addEventListener('click', () => { dayOffset += 1; load(); });
  el.today.addEventListener('click', () => { dayOffset = 0; scrolledToNow = false; load(); });
  el.who.addEventListener('input', () => { auth?.setLocalName(el.who.value); render(); });
  window.addEventListener('resize', renderNowLine);

  // 根据登录态切换「填姓名」和「去登录」两种形态
  const boardLogin = document.getElementById('board-login');
  const whoNameLabel = document.getElementById('who-name-label');
  boardLogin?.addEventListener('click', async () => {
    try { await auth.login(); } catch (error) { window.alert(error.message); }
  });
  auth?.onChange((session) => {
    const loggedIn = !!session.user;
    const needsLogin = !!session.sso_enabled && !loggedIn;
    el.who.hidden = needsLogin || loggedIn;
    if (whoNameLabel) whoNameLabel.hidden = needsLogin || loggedIn;
    if (boardLogin) boardLogin.hidden = !needsLogin;
    if (loggedIn) el.who.value = session.user.name || '';
    const roleHint = document.getElementById('booking-role-hint');
    if (roleHint) {
      roleHint.textContent = session.is_admin
        ? '管理员可取消任意预约'
        : '普通用户只能取消自己的预约';
    }
    render();
    load();
  });

  el.who.value = identity.name;
  el.board.style.setProperty('--booking-slots', String(SLOTS));
  renderHours();
  load();
  // 现在时刻线每分钟走一格，和大盘刷新解耦
  setInterval(renderNowLine, 60000);
  setInterval(load, 120000);
})();
