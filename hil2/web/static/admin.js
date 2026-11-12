/* 管理后台。
 * 全部写操作都经 auth.fetchAuthed 带上登录令牌；
 * 后端会再校验一次管理员身份，前端隐藏按钮只是体验，不是安全边界。
 */
(() => {
  'use strict';

  const auth = window.HIL2Auth;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#${name}"/></svg>`;

  let overview = null;
  let clusters = [];
  let modes = [];
  let editing = null;

  /* ---------------- 字段定义 ---------------- */
  // group 为 ssh 的字段只在 SSH 拉取模式下显示
  const CLUSTER_FIELDS = [
    { key: 'cluster', label: '集群编号', hint: '一个编号 = 一台完整集群，不是一张 GPU 卡', required: true },
    { key: 'name', label: '显示名称', hint: '例如「整车台架 A」，只影响展示' },
    { key: 'mode', label: '采集方式', type: 'select', hint: '两种方式产出的事件完全一样' },
    { key: 'enabled', label: '启用采集', type: 'bool' },
    { key: 'managed', label: '纳入大盘统计', type: 'bool', hint: '关掉后仍会采集，但不计入汇总' },

    { key: 'ssh_host', label: 'SSH 主机', group: 'ssh', hint: '主机名或 IP' },
    { key: 'ssh_port', label: 'SSH 端口', group: 'ssh', type: 'number' },
    { key: 'ssh_user', label: 'SSH 用户', group: 'ssh', hint: '建议用只读服务账号，不要用 root' },
    { key: 'ssh_key_path', label: '私钥路径', group: 'ssh', hint: '协调服务本机的绝对路径，留空则用默认密钥' },
    { key: 'ssh_jump_host', label: '堡垒机主机（可选）', group: 'ssh',
      hint: '客户环境填写；测试环境没有堡垒机可以留空' },
    { key: 'ssh_jump_port', label: '堡垒机端口', group: 'ssh', type: 'number' },
    { key: 'ssh_jump_user', label: '堡垒机用户', group: 'ssh', hint: '留空则使用 SSH 默认用户' },
    { key: 'ssh_jump_key_path', label: '堡垒机私钥路径', group: 'ssh',
      hint: '可与目标机私钥不同；必须是协调服务本机绝对路径' },
    { key: 'ssh_python', label: '远端 Python', group: 'ssh', hint: '需要 3.8+，采集脚本零依赖' },
    { key: 'ssh_use_sudo', label: '用 sudo 读 docker', group: 'ssh', type: 'bool',
      hint: '账号不在 docker 组时才勾；需配好最小 sudo 规则' },

    { key: 'data_dir', label: '数据目录', hint: '测试产出写在哪里，用于判断「近期有无写入」' },
    { key: 'idle_minutes', label: '无活动窗口（分钟）', type: 'number',
      hint: '多久没有测试进程和数据写入才算可疑' },
    { key: 'sample_seconds', label: '采集间隔（秒）', type: 'number' },
    { key: 'confirm_samples', label: '确认轮数', type: 'number',
      hint: '连续几轮可疑才打扰当前占用人' },
    { key: 'command_timeout', label: '命令超时（秒）', type: 'number' },

    { key: 'sim_pattern', label: 'sim 容器规则', mono: true,
      hint: '正则。现场确认后建议改成精确名，例如 ^(hil-sim-1|hil-sim-2)$' },
    { key: 'proxy_pattern', label: 'proxy 容器规则', mono: true },
    { key: 'tester_pattern', label: 'tester 容器规则', mono: true },
    { key: 'extra_test_processes', label: '其他测试进程', mono: true,
      hint: 'pytest 之外的，逗号分隔。宁可先不填也别乱填——填错会让可疑判定永远不触发' },

    { key: 'webhook_url', label: '监控群 Webhook', hint: '留空则用全局配置。含密钥，保存后不回显' },
    { key: 'note', label: '备注', type: 'textarea' }
  ];

  const STATE_TONE = {
    busy: 'busy', idle: 'idle', reserved: 'reserved',
    suspected_idle: 'suspected_idle', unknown: 'unknown'
  };

  /* ---------------- 通用 ---------------- */
  async function api(path, options = {}) {
    const response = await auth.fetchAuthed(path, options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${response.status}`);
    }
    return response.status === 204 ? null : response.json();
  }

  function toast(message, ok = true) {
    const node = document.createElement('div');
    node.className = `toast${ok ? '' : ' toast--bad'}`;
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(() => node.remove(), 3600);
  }

  /* ---------------- 集群 ---------------- */
  function clusterRow(item) {
    const isSsh = item.mode === 'ssh_pull';
    const tone = STATE_TONE[item.current_state] || 'no_data';
    const badges = [];
    if (!item.enabled) badges.push('<span class="tag">已暂停</span>');
    if (!item.managed) badges.push('<span class="tag">不计入汇总</span>');
    if (item.last_error) badges.push(`<span class="tag tag--warn">采集报错</span>`);

    return `<article class="crow" data-state="${tone}" data-cluster="${esc(item.cluster)}">
      <div class="crow__id">
        <span class="crow__no">${esc(item.cluster)}</span>
        <span class="crow__name">${esc(item.name || '未命名')}</span>
      </div>
      <div class="crow__mode">
        <span class="pill">${icon(isSsh ? 'i-plug' : 'i-server')}${esc(item.mode_label)}</span>
        ${isSsh ? `<span class="crow__meta num">${esc(item.ssh_user ? item.ssh_user + '@' : '')}${esc(item.ssh_host)}</span>`
                : `<span class="crow__meta">集群上常驻 agent</span>`}
      </div>
      <div class="crow__state">
        <span class="pill">${esc(item.current_state_label)}</span>
        ${badges.join('')}
        ${item.last_error ? `<div class="crow__err">${esc(item.last_error)}</div>` : ''}
      </div>
      <div class="crow__actions">
        ${isSsh ? `<button type="button" class="ghost ghost--sm" data-act="test">测试连接</button>
                   <button type="button" class="ghost ghost--sm" data-act="collect">立即采集</button>`
                : `<button type="button" class="ghost ghost--sm" data-act="download">
                     ${icon('i-download')}下载安装包</button>`}
        <button type="button" class="ghost ghost--sm" data-act="edit">配置</button>
      </div>
    </article>`;
  }

  function renderClusters() {
    const list = $('cluster-list');
    $('cluster-empty').hidden = clusters.length > 0;
    list.innerHTML = clusters.map(clusterRow).join('');

    const byMode = clusters.reduce((acc, c) => {
      acc[c.mode] = (acc[c.mode] || 0) + 1; return acc;
    }, {});
    $('cluster-summary').textContent =
      `共 ${clusters.length} 台 · Agent 推送 ${byMode.agent_push || 0} · SSH 拉取 ${byMode.ssh_pull || 0}`;

    list.querySelectorAll('[data-act]').forEach((button) => {
      button.addEventListener('click', () => {
        const cluster = button.closest('.crow').dataset.cluster;
        const act = button.dataset.act;
        if (act === 'edit') return openCluster(cluster);
        if (act === 'test') return testCluster(cluster, button);
        if (act === 'collect') return collectNow(cluster, button);
        if (act === 'download') return downloadAgent(cluster, button);
        return undefined;
      });
    });
  }

  function fieldHtml(field, value, mode) {
    if (field.group === 'ssh' && mode !== 'ssh_pull') return '';
    const id = `f-${field.key}`;
    const hint = field.hint ? `<small>${esc(field.hint)}</small>` : '';
    const cls = field.mono ? ' class="num"' : '';

    if (field.type === 'bool') {
      return `<label class="field field--check">
        <input type="checkbox" id="${id}" data-key="${field.key}" ${value ? 'checked' : ''}>
        <span><b>${esc(field.label)}</b>${hint}</span></label>`;
    }
    if (field.type === 'select') {
      const options = modes.map((m) =>
        `<option value="${m.value}"${m.value === value ? ' selected' : ''}>${esc(m.label)}</option>`
      ).join('');
      return `<label class="field"><span>${esc(field.label)}</span>
        <select id="${id}" data-key="${field.key}">${options}</select>${hint}</label>`;
    }
    if (field.type === 'textarea') {
      return `<label class="field field--wide"><span>${esc(field.label)}</span>
        <textarea id="${id}" data-key="${field.key}" rows="2">${esc(value ?? '')}</textarea>${hint}</label>`;
    }
    const type = field.type === 'number' ? 'number' : 'text';
    const readonly = field.key === 'cluster' && editing?.existing ? ' readonly' : '';
    return `<label class="field${field.mono ? ' field--wide' : ''}"><span>${esc(field.label)}</span>
      <input type="${type}" id="${id}" data-key="${field.key}"${cls}${readonly}
             value="${esc(value ?? '')}">${hint}</label>`;
  }

  function renderClusterForm() {
    const data = editing.data;
    $('cluster-fields').innerHTML =
      CLUSTER_FIELDS.map((f) => fieldHtml(f, data[f.key], data.mode)).join('');
    // 切换采集方式时立刻显隐 SSH 字段，不用等保存
    $('f-mode')?.addEventListener('change', (event) => {
      editing.data = { ...collectForm(), mode: event.target.value };
      renderClusterForm();
    });
  }

  function collectForm() {
    const out = {};
    $('cluster-fields').querySelectorAll('[data-key]').forEach((node) => {
      out[node.dataset.key] = node.type === 'checkbox' ? node.checked : node.value;
    });
    return out;
  }

  function openCluster(cluster) {
    const existing = clusters.find((c) => c.cluster === cluster);
    editing = {
      existing: !!existing,
      original: cluster || '',
      data: existing ? { ...existing } : {
        cluster: '', name: '', mode: 'agent_push', enabled: true, managed: true,
        data_dir: '/data1', idle_minutes: 15, sample_seconds: 300, confirm_samples: 3,
        command_timeout: 30, ssh_port: 22, ssh_jump_port: 22, ssh_python: 'python3',
        sim_pattern: '(^|[-_])sim(ulator)?($|[-_])',
        proxy_pattern: '(^|[-_])proxy($|[-_])',
        tester_pattern: 'tester|autotest|simlib|logsim',
        extra_test_processes: '', webhook_url: '', note: ''
      }
    };
    $('cluster-sheet-title').textContent = existing ? `配置 ${cluster}` : '新增集群';
    $('cluster-delete').hidden = !existing;
    $('cluster-error').hidden = true;
    renderClusterForm();
    $('cluster-sheet').showModal();
  }

  async function saveCluster() {
    const payload = collectForm();
    const error = $('cluster-error');
    error.hidden = true;
    try {
      if (editing.existing) {
        await api(`/api/admin/clusters/${encodeURIComponent(editing.original)}`,
          { method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload) });
      } else {
        await api('/api/admin/clusters',
          { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload) });
      }
      $('cluster-sheet').close();
      toast('已保存');
      await loadClusters();
    } catch (exc) {
      error.hidden = false;
      error.textContent = exc.message;
    }
  }

  async function testCluster(cluster, button) {
    button.disabled = true;
    button.textContent = '连接中…';
    try {
      const { data } = await api(`/api/admin/clusters/${encodeURIComponent(cluster)}/test`,
        { method: 'POST' });
      showTest(cluster, data);
      await loadClusters();
    } catch (exc) {
      toast(exc.message, false);
    } finally {
      button.disabled = false;
      button.textContent = '测试连接';
    }
  }

  function showTest(cluster, data) {
    $('test-title').textContent = `${cluster} 连接测试`;
    const containers = Object.entries(data.containers || {})
      .map(([role, items]) => `<tr><td class="num">${esc(role)}</td><td>${
        items.length
          ? items.map((i) => `${esc(i.name)}<span class="dimmed">（${esc(i.state)}）</span>`).join('、')
          : '<span class="warnish">未匹配到容器</span>'
      }</td></tr>`).join('');

    $('test-body').innerHTML = `
      <p class="testline ${data.ok ? 'ok' : 'bad'}">
        ${icon(data.ok ? 'i-check' : 'i-alert')}
        ${data.ok ? `连接成功，耗时 ${data.elapsed_ms} ms` : esc(data.error)}
      </p>
      <dl class="sheet__facts">
        <div><dt>判定结果</dt><dd>${esc(data.state_label)}</dd></div>
        <div><dt>主机名</dt><dd class="num">${esc(data.hostname || '—')}</dd></div>
        <div><dt>耗时</dt><dd class="num">${data.elapsed_ms} ms</dd></div>
      </dl>
      ${data.reasons?.length ? `<p class="hint">依据：${data.reasons.map(esc).join('；')}</p>` : ''}
      ${containers ? `<table class="minitable"><tbody>${containers}</tbody></table>` : ''}
      ${!data.ok ? `<p class="hint"><b>采集失败一律判为「采集异常」，绝不会被当成空闲。</b>
        常见原因：SSH 免密没配好、账号读不到 docker、远端没有 Python 3.8+。</p>` : ''}`;
    $('test-sheet').showModal();
  }

  async function collectNow(cluster, button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = '采集中…';
    try {
      const { data } = await api(`/api/admin/clusters/${encodeURIComponent(cluster)}/collect`,
        { method: 'POST' });
      toast(data.error ? `采集异常：${data.error}` : `采集完成：${data.state}`, !data.error);
      await loadClusters();
    } catch (exc) {
      toast(exc.message, false);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function downloadAgent(cluster, button) {
    button.disabled = true;
    try {
      const response = await auth.fetchAuthed(
        `/api/admin/clusters/${encodeURIComponent(cluster)}/agent.tar.gz`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `hil-agent-${cluster}.tar.gz`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast('安装包已下载。包内含上报密钥，请勿转发');
    } catch (exc) {
      toast(exc.message, false);
    } finally {
      button.disabled = false;
    }
  }

  async function loadClusters() {
    const body = await api('/api/admin/clusters');
    clusters = body.data;
    modes = body.modes;
    renderClusters();
  }

  /* ---------------- 组织 ---------------- */
  async function loadOrg() {
    const keyword = $('org-search').value.trim();
    const adminsOnly = $('org-admins-only').checked;
    const body = await api(
      `/api/admin/org/users?q=${encodeURIComponent(keyword)}&admins_only=${adminsOnly}`);
    const users = body.data;
    $('org-empty').hidden = users.length > 0;
    $('org-list').innerHTML = users.map((user) => `
      <article class="orow">
        <div class="orow__who">
          ${user.avatar_url ? `<img src="${esc(user.avatar_url)}" alt="" class="orow__avatar">`
                            : `<span class="orow__avatar orow__avatar--blank">${esc((user.name || '?')[0])}</span>`}
          <div>
            <b>${esc(user.name || user.open_id)}</b>
            <small>${esc(user.department_names.join(' / ') || '—')}</small>
          </div>
        </div>
        <div class="orow__contact num">${esc(user.email || user.mobile || '—')}</div>
        <label class="checkline">
          <input type="checkbox" data-admin="${esc(user.open_id)}" ${user.is_admin ? 'checked' : ''}>
          管理员
        </label>
      </article>`).join('');

    $('org-list').querySelectorAll('[data-admin]').forEach((box) => {
      box.addEventListener('change', async () => {
        try {
          await api(`/api/admin/org/users/${encodeURIComponent(box.dataset.admin)}/admin`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_admin: box.checked })
          });
          toast(box.checked ? '已设为管理员' : '已取消管理员');
        } catch (exc) {
          box.checked = !box.checked;
          toast(exc.message, false);
        }
      });
    });
  }

  function renderOrgSummary() {
    const org = overview?.org || {};
    const last = org.last_synced_at
      ? new Date(org.last_synced_at).toLocaleString('zh-CN')
      : '从未同步';
    $('org-summary').textContent =
      `${org.users || 0} 人 · ${org.departments || 0} 个部门 · ${org.admins || 0} 名管理员 · 最近同步 ${last}`;
  }

  /* ---------------- 设置 ---------------- */
  function renderSettings() {
    const editable = overview.editable_settings || {};
    const values = overview.settings || {};
    $('settings-form').innerHTML = Object.entries(editable).map(([key, label]) => {
      const value = values[key];
      const isNumber = typeof value === 'number';
      return `<label class="field${isNumber ? '' : ' field--wide'}">
        <span>${esc(label)}</span>
        <input type="${isNumber ? 'number' : 'text'}" data-setting="${key}"
               class="num" value="${esc(value ?? '')}">
        <small>${esc(key)}</small></label>`;
    }).join('');

    $('audit-list').innerHTML = (overview.audit || []).map((item) => `
      <li><span class="feed__kind">${esc(item.action)}</span>
        <span class="feed__note">${esc(item.target || '—')} ${esc(item.detail || '')}</span>
        <span class="feed__when">${item.created_at
          ? new Date(item.created_at).toLocaleString('zh-CN') : ''}</span></li>`).join('');
  }

  async function saveSettings() {
    const payload = {};
    $('settings-form').querySelectorAll('[data-setting]').forEach((node) => {
      payload[node.dataset.setting] = node.type === 'number' ? Number(node.value) : node.value;
    });
    try {
      await api('/api/admin/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      toast('设置已保存，立即生效');
      await loadOverview();
    } catch (exc) {
      toast(exc.message, false);
    }
  }

  /* ---------------- 启动 ---------------- */
  async function loadOverview() {
    overview = await api('/api/admin/overview');
    renderOrgSummary();
    renderSettings();
  }

  function switchTab(name) {
    document.querySelectorAll('.tabs__item').forEach((tab) => {
      tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
    });
    ['clusters', 'org', 'settings'].forEach((key) => {
      $(`tab-${key}`).hidden = key !== name;
    });
    if (name === 'org') loadOrg().catch((e) => toast(e.message, false));
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('hil2-theme', theme); } catch { /* 忽略 */ }
  }

  async function boot() {
    try { applyTheme(localStorage.getItem('hil2-theme') || 'light'); } catch { /* 忽略 */ }
    $('theme-toggle').addEventListener('click', () =>
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

    const session = await auth.refresh();
    if (session.user) {
      $('who').hidden = false;
      $('who-label').textContent = session.user.name || session.user.open_id.slice(0, 10);
    }

    try {
      await Promise.all([loadOverview(), loadClusters()]);
      $('console').hidden = false;
      $('gate').hidden = true;
    } catch (exc) {
      $('gate').hidden = false;
      $('gate-text').innerHTML = `<b>进不去管理后台。</b>${esc(exc.message)}`;
      $('gate-login').hidden = !session.sso_enabled || !!session.user;
      return;
    }

    document.querySelectorAll('.tabs__item').forEach((tab) => {
      tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });
    $('add-cluster').addEventListener('click', () => openCluster(''));
    $('cluster-save').addEventListener('click', saveCluster);
    $('cluster-cancel').addEventListener('click', () => $('cluster-sheet').close());
    $('cluster-close').addEventListener('click', () => $('cluster-sheet').close());
    $('cluster-delete').addEventListener('click', async () => {
      if (!window.confirm(`把 ${editing.original} 移出纳管？\n历史事件和预约会保留。`)) return;
      try {
        await api(`/api/admin/clusters/${encodeURIComponent(editing.original)}`, { method: 'DELETE' });
        $('cluster-sheet').close();
        toast('已移出纳管');
        await loadClusters();
      } catch (exc) { toast(exc.message, false); }
    });
    $('test-close').addEventListener('click', () => $('test-sheet').close());
    $('test-ok').addEventListener('click', () => $('test-sheet').close());
    $('save-settings').addEventListener('click', saveSettings);
    $('org-sync').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      const original = button.innerHTML;
      button.textContent = '同步中…';
      try {
        const { data } = await api('/api/admin/org/sync', { method: 'POST' });
        toast(`同步完成：${data.departments} 个部门，${data.users} 人`);
        if (data.warnings?.length) toast(data.warnings[0], false);
        await Promise.all([loadOverview(), loadOrg()]);
      } catch (exc) { toast(exc.message, false); }
      finally { button.disabled = false; button.innerHTML = original; }
    });
    $('org-check').addEventListener('click', async () => {
      try {
        const { data } = await api('/api/admin/org/check');
        toast(data.ok
          ? `连接正常，根部门 ${data.root_departments} 个：${(data.sample || []).join('、')}`
          : data.error, data.ok);
      } catch (exc) { toast(exc.message, false); }
    });

    let searchTimer = null;
    $('org-search').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadOrg().catch(() => {}), 250);
    });
    $('org-admins-only').addEventListener('change', () => loadOrg().catch(() => {}));
    $('gate-login').addEventListener('click', () => auth.login().catch((e) => toast(e.message, false)));
  }

  boot();
})();
