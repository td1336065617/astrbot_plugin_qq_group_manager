/**
 * QQ群管理 · WebUI 管理台（单页 SPA，hash 路由）
 *
 * 依赖 AstrBot 注入的 window.AstrBotPluginPage（bridge-sdk）：
 *   await bridge.ready() / bridge.apiGet(endpoint, params) / bridge.apiPost(endpoint, body)
 *   bridge.subscribeSSE(endpoint, handlers, {topic})
 *
 * 约定：endpoint 为插件内相对路径，不带插件名、不带前导斜杠。
 * 注意：不使用模板字符串与任何 CDN 依赖，保持零构建。
 */

const bridge = window.AstrBotPluginPage;
const PLUGIN = 'astrbot_plugin_qq_group_manager';

const state = {
  config: null,
  summary: null,
  logs: { kind: 'api', page: 1, page_size: 20, data: null, filters: {} },
  sse: null,
  sseLines: [],
  busy: false,
};

const VIEWS = [
  { id: 'dashboard', label: '总览', icon: '📊' },
  { id: 'groups', label: '群管理', icon: '👥' },
  { id: 'logs', label: '日志中心', icon: '🧾' },
  { id: 'tools', label: '工具', icon: '🧰' },
  { id: 'policy', label: '策略', icon: '⚙️' },
  { id: 'keywords', label: '关键词', icon: '🔤' },
  { id: 'rulesx', label: '规则增强', icon: '🧬' },
  { id: 'members', label: '成员与禁言', icon: '🚫' },
  { id: 'joins', label: '入群审批', icon: '🚪' },
  { id: 'appeals', label: '申诉', icon: '⚖️' },
];

const LOG_TABS = [
  { kind: 'events', label: '审核事件' },
  { kind: 'actions', label: '动作执行' },
  { kind: 'api', label: 'API 调用' },
  { kind: 'capability', label: '能力受限' },
];

/* --------------------------------------------------------------- 模态框 */

/**
 * AstrBot 插件页面运行在 sandbox iframe 中（仅 allow-scripts allow-forms allow-downloads），
 * 原生 window.confirm / alert / prompt 会被浏览器忽略并直接返回 false —— 这正是
 * 「点按钮没反应」的原因。这里用自绘模态框完全替代它们。
 */
function uiModal(options) {
  const opts = options || {};
  return new Promise((resolve) => {
    const overlay = el('div', { class: 'modal-overlay' });
    const box = el('div', { class: 'modal-box' });
    box.appendChild(el('h3', { text: opts.title || '请确认' }));
    if (opts.body) box.appendChild(el('div', { class: 'modal-body', text: opts.body }));
    let input = null;
    if (opts.input) {
      input = el('input', { type: 'text', value: opts.defaultValue || '' });
      box.appendChild(input);
    }
    const onKey = (event) => {
      if (event.key === 'Escape') close(null);
      if (event.key === 'Enter' && input) close(input.value);
    };
    const close = (value) => {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const actions = el('div', { class: 'modal-actions' });
    if (!opts.hideCancel) {
      actions.appendChild(el('button', {
        class: 'btn ghost',
        text: opts.cancelText || '取消',
        onclick: () => close(null),
      }));
    }
    actions.appendChild(el('button', {
      class: 'btn',
      text: opts.confirmText || '确认',
      onclick: () => close(input ? input.value : true),
    }));
    box.appendChild(actions);
    overlay.appendChild(box);
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) close(null);
    });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
    if (input) input.focus();
  });
}

async function uiConfirm(message, confirmText) {
  return (await uiModal({ title: '请确认', body: message, confirmText: confirmText || '确认' })) === true;
}

async function uiPrompt(message, defaultValue) {
  const value = await uiModal({
    title: '请输入',
    body: message,
    input: true,
    defaultValue: defaultValue || '',
    confirmText: '确定',
  });
  return value === null || value === undefined ? null : String(value);
}

async function uiNotice(title, body) {
  await uiModal({ title: title || '提示', body: body, confirmText: '知道了', hideCancel: true });
}

/* ------------------------------------------------------------------ 工具 */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach((key) => {
      const value = attrs[key];
      if (value === undefined || value === null) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'dataset') {
        Object.keys(value).forEach((k) => { node.dataset[k] = value[k]; });
      } else node.setAttribute(key, value);
    });
  }
  (children || []).forEach((child) => {
    if (child === null || child === undefined) return;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  });
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function toast(message, kind) {
  const box = document.getElementById('toast');
  box.textContent = message;
  box.className = 'toast ' + (kind || '');
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { box.hidden = true; }, 4200);
}

function card(title, desc, children) {
  return el('section', { class: 'card' }, [
    el('h2', { text: title }),
    desc ? el('p', { class: 'card-desc', text: desc }) : null,
  ].concat(children || []));
}

function notice(text, kind) {
  return el('div', { class: 'notice ' + (kind || ''), text });
}

function tag(text, kind) {
  return el('span', { class: 'tag ' + (kind || ''), text });
}

function fmtProfileNumber(profile, key) {
  const value = profile ? profile[key] : null;
  if (value === null || value === undefined || value === '') {
    return profile && profile.degraded ? '本通道不支持' : '—';
  }
  return String(value);
}

function fmtProfileAge(profile) {
  const value = profile ? profile.account_age_days : null;
  if (value === null || value === undefined) {
    return profile && profile.degraded ? '本通道不支持' : '—';
  }
  return value + ' 天';
}

function fmtTime(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 19);
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 14 ? text.slice(0, 6) + '…' + text.slice(-4) : text;
}

async function loadConfig(force) {
  if (state.config && !force) return state.config;
  state.config = await bridge.apiGet('config');
  return state.config;
}

async function loadSummary() {
  state.summary = await bridge.apiGet('summary', { days: 1 });
  return state.summary;
}

/* ------------------------------------------------------------------ 顶栏 */

function renderTopbar() {
  const badges = document.getElementById('badges');
  const runtime = (state.config && state.config.runtime) || {};
  const transport = runtime.transport || {};
  const queue = runtime.db_queue || {};
  clear(badges);
  document.getElementById('version').textContent = 'v' + (runtime.version || '?');

  const items = [];
  items.push({ text: '平台：' + (transport.platform_id || '未连接'), kind: transport.available ? 'ok' : 'bad' });
  items.push({ text: runtime.dry_run ? 'DRY-RUN：只记录不处置' : '已开启实际处置', kind: runtime.dry_run ? 'warn' : 'ok' });
  items.push({ text: '审核群：' + (runtime.groups_moderating || 0) + '/' + (runtime.groups_total || 0), kind: '' });
  items.push({ text: '模式：' + (runtime.mode || '-'), kind: '' });
  if (queue.dropped) items.push({ text: '日志丢弃：' + queue.dropped, kind: 'bad' });
  if (queue.last_error) items.push({ text: '审计写入异常', kind: 'bad' });

  items.forEach((item) => badges.appendChild(el('span', { class: 'badge ' + item.kind, text: item.text })));
}

function renderNav() {
  const nav = document.getElementById('nav');
  const current = location.hash.replace('#/', '') || 'dashboard';
  clear(nav);
  VIEWS.forEach((view) => {
    nav.appendChild(el('a', {
      href: '#/' + view.id,
      class: view.id === current ? 'active' : '',
    }, [
      el('span', { text: view.icon + ' ' + view.label }),
      view.soon ? el('span', { class: 'soon', text: view.soon }) : null,
    ]));
  });
}

/* --------------------------------------------------------------- 总览页 */

function statCard(label, value, hint) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'label', text: label }),
    el('div', { class: 'value', text: String(value) }),
    hint ? el('div', { class: 'label', text: hint }) : null,
  ]);
}

async function viewDashboard(root) {
  const config = await loadConfig();
  const runtime = config.runtime || {};
  const settings = config.settings || {};
  let summary = null;
  try { summary = await loadSummary(); } catch (error) { summary = null; }
  const stats = (summary && summary.stats) || {};
  const verdicts = stats.verdicts || {};
  const actions = stats.actions || {};
  const actionTotal = Object.keys(actions).reduce((acc, key) => acc + (actions[key].ok || 0) + (actions[key].fail || 0), 0);

  clear(root);
  root.appendChild(card('运行状态', '插件当前配置与平台连通性', [
    el('div', { class: 'grid cols-4' }, [
      statCard('平台通道', runtime.transport && runtime.transport.available ? '可用' : '不可用'),
      statCard('群数量', runtime.groups_total || 0, '审核中 ' + (runtime.groups_moderating || 0)),
      statCard(
        '审核模型',
        (config.providers && (config.providers.last_used || config.providers.configured)) || '跟随会话默认',
        '可在「策略」页切换'
      ),
      statCard('今日审核', stats.events_total || 0, '违规 ' + (verdicts.violation || 0) + ' / 可疑 ' + (verdicts.review || 0)),
      statCard('今日处置', actionTotal, '失败 ' + Object.keys(actions).reduce((acc, k) => acc + (actions[k].fail || 0), 0)),
    ]),
  ]));

  const dryRun = el('input', { type: 'checkbox' });
  dryRun.checked = !!settings.dry_run;
  const modeSelect = el('select');
  (config.options && config.options.modes ? config.options.modes : ['lenient']).forEach((mode) => {
    modeSelect.appendChild(el('option', { value: mode, text: mode, selected: mode === settings.mode ? 'selected' : null }));
  });
  const saveBtn = el('button', { class: 'btn', text: '保存运行开关' , onclick: async () => {
    saveBtn.disabled = true;
    try {
      await bridge.apiPost('config', { section: 'settings', data: { dry_run: dryRun.checked, mode: modeSelect.value } });
      state.config = null;
      toast('已保存', 'ok');
      await render();
    } catch (error) {
      toast('保存失败：' + error.message, 'bad');
    } finally { saveBtn.disabled = false; }
  } });

  root.appendChild(card('运行开关', '首次安装默认 dry-run + lenient（只记录、只警告）。确认判定准确后再关闭 dry-run。', [
    el('div', { class: 'row' }, [
      el('label', { class: 'field' }, [el('span', { text: 'dry-run（只记录不处置）' }), dryRun]),
      el('label', { class: 'field' }, [el('span', { text: '默认模式' }), modeSelect]),
      el('div', { class: 'field-actions' }, [saveBtn]),
    ]),
  ]));

  if (runtime.dry_run) {
    root.appendChild(notice('当前处于 dry-run：撤回 / 禁言 / 拉黑 / 移除只写入审计日志不会执行；警告与上报仍会真实发送（可在「策略」页用 dry_run_warn 关闭）。', 'warn'));
  }
  if (!(runtime.transport && runtime.transport.available)) {
    root.appendChild(notice('未检测到 qq_official 平台通道：请在 AstrBot 中启用 QQ 官方机器人适配器，并让机器人在群里收到一条消息后重试。', 'bad'));
  }

  const alerts = (stats.capability_denied || []).map((item) => item.capability + '：err_code=' + item.err_code + ' ×' + item.count);
  if (alerts.length) {
    root.appendChild(card('能力受限（近 24 小时）', '受限不代表插件异常：平台未授权或能力仍在灰度。详细建议见「工具 → 能力自检」。', [
      el('div', { class: 'grid cols-2' }, alerts.map((text) => el('div', { class: 'notice warn', text }))),
    ]));
  }

  const tasks = (summary && summary.tasks) || [];
  const tbody = el('tbody');
  tasks.forEach((task) => {
    tbody.appendChild(el('tr', {}, [
      el('td', { text: task.name }),
      el('td', { text: String(task.interval) + 's' }),
      el('td', { text: String(task.runs) }),
      el('td', {}, [task.failures ? tag('失败 ' + task.failures, 'bad') : tag('正常', 'ok')]),
      el('td', { text: task.last_error || '—' }),
    ]));
  });
  root.appendChild(card('后台任务', '任务在插件初始化时启动，重载插件会重建。', [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['任务', '周期', '执行次数', '状态', '最近错误'].map((text) => el('th', { text })))]),
        tbody,
      ]),
    ]),
  ]));
}

/* ------------------------------------------------------------- 群管理页 */

function capabilityTags(group, options) {
  const caps = group.capabilities || {};
  const wrap = el('div', { class: 'row' });
  (options.capabilities || []).forEach((item) => {
    const record = caps[item.key];
    if (!record) return;
    const kind = record.probed === false ? 'warn' : (record.ok ? 'ok' : 'bad');
    const title = record.note || (record.err_code ? 'err_code=' + record.err_code : '');
    wrap.appendChild(el('span', { class: 'tag ' + kind, title, text: item.label }));
  });
  return wrap;
}

async function viewGroups(root) {
  const config = await loadConfig();
  const options = config.options || {};
  const groups = config.groups || [];
  clear(root);

  const idInput = el('input', { type: 'text', placeholder: 'group_openid（可从群消息日志或平台获取）' });
  const nameInput = el('input', { type: 'text', placeholder: '备注名（可选）' });
  const addBtn = el('button', { class: 'btn ghost', text: '添加群', onclick: async () => {
    if (!idInput.value.trim()) { toast('请填写 group_openid', 'bad'); return; }
    addBtn.disabled = true;
    try {
      await bridge.apiPost('groups/add', { group_id: idInput.value.trim(), name: nameInput.value.trim() });
      idInput.value = ''; nameInput.value = '';
      state.config = null;
      toast('已添加，建议立即执行能力探测', 'ok');
      await render();
    } catch (error) { toast('添加失败：' + error.message, 'bad'); }
    finally { addBtn.disabled = false; }
  } });
  const probeAllBtn = el('button', { class: 'btn ghost', text: '全部重探', onclick: async () => {
    probeAllBtn.disabled = true;
    try {
      await bridge.apiPost('groups/probe', { all: true });
      state.config = null;
      toast('能力探测完成', 'ok');
      await render();
    } catch (error) { toast('探测失败：' + error.message, 'bad'); }
    finally { probeAllBtn.disabled = false; }
  } });

  root.appendChild(card('群列表', '群在收到消息后会自动登记；也可手动添加。启用审核前必须先开启「接收全部消息」。', [
    el('div', { class: 'row' }, [idInput, nameInput, el('div', { class: 'field-actions' }, [addBtn, probeAllBtn])]),
  ]));

  if (!groups.length) {
    root.appendChild(notice('还没有记录任何群：把机器人拉进群并在群里 @ 一次机器人，或在上方手动添加 group_openid。'));
    return;
  }

  const tbody = el('tbody');
  groups.forEach((group) => {
    const probeBtn = el('button', { class: 'btn small ghost', text: '探测', onclick: async () => {
      probeBtn.disabled = true;
      try {
        await bridge.apiPost('groups/probe', { group_id: group.group_id });
        state.config = null;
        toast('已重新探测', 'ok');
        await render();
      } catch (error) { toast('探测失败：' + error.message, 'bad'); }
      finally { probeBtn.disabled = false; }
    } });

    const toggleBtn = el('button', {
      class: 'btn small ' + (group.moderation_enabled ? 'ghost' : ''),
      text: group.moderation_enabled ? '停用审核' : '启用审核',
      onclick: async () => {
        toggleBtn.disabled = true;
        const enable = !group.moderation_enabled;
        if (enable) {
          const ok = await uiConfirm('启用审核将对该群的全部消息做 LLM 判定（消耗 token）。确认继续？');
          if (!ok) { toggleBtn.disabled = false; return; }
        }
        try {
          await bridge.apiPost('groups/moderation', { group_id: group.group_id, enable });
          state.config = null;
          toast(enable ? '审核已启用' : '审核已停用', 'ok');
          await render();
        } catch (error) {
          let message = error.message;
          try {
            const parsed = JSON.parse(message);
            if (parsed && parsed.data && parsed.data.reason_code === 'need_full_msg') {
              message = parsed.message || parsed.data.message;
            }
          } catch (ignore) { /* 非 JSON 错误 */ }
          toast('操作失败：' + message, 'bad');
          await uiNotice('启用审核失败', message);
        } finally { toggleBtn.disabled = false; }
      },
    });

    const removeBtn = el('button', { class: 'btn small danger', text: '移除记录', onclick: async () => {
      if (!(await uiConfirm('仅移除插件侧的群记录，不影响平台与群成员。确认？'))) return;
      removeBtn.disabled = true;
      try {
        await bridge.apiPost('groups/remove', { group_id: group.group_id });
        state.config = null;
        toast('已移除', 'ok');
        await render();
      } catch (error) { toast('移除失败：' + error.message, 'bad'); }
      finally { removeBtn.disabled = false; }
    } });

    const stateTags = [];
    stateTags.push(group.moderation_enabled ? tag('审核中', 'ok') : tag('未开启审核', ''));
    if (group.paused_reason) stateTags.push(tag('已暂停：' + group.paused_reason, 'warn'));
    stateTags.push(group.cap_is_admin ? tag('群管理员', 'ok') : tag('非管理员', 'warn'));
    stateTags.push(group.cap_full_msg ? tag('全量消息', 'ok') : tag('仅 @消息', 'warn'));

    tbody.appendChild(el('tr', {}, [
      el('td', {}, [
        el('div', { text: group.name || '（未获取群名）' }),
        el('div', { class: 'muted mono', text: shortId(group.group_id), title: group.group_id }),
      ]),
      el('td', {}, stateTags),
      el('td', {}, [capabilityTags(group, options)]),
      el('td', {}, [(() => {
        const modeSelect = el('select');
        modeSelect.appendChild(el('option', {
          value: '',
          text: '跟随全局（' + ((config.settings || {}).mode || '') + '）',
          selected: !group.mode ? 'selected' : null,
        }));
        (config.options && config.options.modes ? config.options.modes : []).forEach((mode) => {
          modeSelect.appendChild(el('option', {
            value: mode,
            text: mode,
            selected: group.mode === mode ? 'selected' : null,
          }));
        });
        modeSelect.addEventListener('change', async () => {
          try {
            await bridge.apiPost('groups/mode', { group_id: group.group_id, mode: modeSelect.value });
            state.config = null;
            toast('本群模式已更新为：' + (modeSelect.value || '跟随全局'), 'ok');
            await render();
          } catch (error) {
            toast('更新失败：' + ((error && error.message) || error), 'bad');
          }
        });
        return modeSelect;
      })()]),
      el('td', { text: group.last_seen_iso ? fmtTime(group.last_seen_iso) : '—' }),
      el('td', {}, [el('div', { class: 'field-actions' }, [toggleBtn, probeBtn, removeBtn])]),
    ]));
  });

  root.appendChild(card('群明细', '能力标签：绿色=可用，红色=受限（悬停查看 err_code 与原因），黄色=无只读探测接口。', [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['群', '状态', '平台能力', '模式', '最近活跃', '操作'].map((text) => el('th', { text })))]),
        tbody,
      ]),
    ]),
  ]));
}

/* ------------------------------------------------------------- 日志中心 */

function logColumns(kind) {
  if (kind === 'events') return ['ts', 'group_id', 'sender_name', 'verdict', 'category', 'severity', 'confidence', 'reason', 'appeal_state'];
  if (kind === 'actions') return ['ts', 'group_id', 'action', 'target_openid', 'ok', 'err_code', 'dry_run'];
  if (kind === 'api') return ['ts_unix', 'group_id', 'method', 'path', 'ok', 'err_code', 'caller', 'duration_ms'];
  return ['ts_unix', 'group_id', 'capability', 'ok', 'err_code', 'note'];
}

function cellValue(kind, key, row) {
  const value = row[key];
  if (key === 'ts' || key === 'ts_unix') {
    const raw = row.ts || (row.ts_unix ? new Date(row.ts_unix * 1000).toISOString() : '');
    return fmtTime(raw);
  }
  if (key === 'group_id' || key === 'target_openid') return shortId(value);
  if (key === 'ok') return value ? '成功' : '失败';
  if (key === 'dry_run') return value ? 'dry-run' : '';
  if (key === 'confidence' && typeof value === 'number') return value.toFixed(2);
  if (key === 'path') return String(value || '').replace('/v2/groups/{group_openid}', '');
  if (key === 'appeal_state') {
    const labels = { pending: '待处理', accepted: '已通过', rejected: '已驳回' };
    return labels[value] || (value ? String(value) : '—');
  }
  return value === null || value === undefined ? '—' : String(value);
}

async function loadLogs(kind, page) {
  const query = Object.assign({ page: page || 1, page_size: state.logs.page_size }, state.logs.filters || {});
  clear(document.getElementById('content'));
  const content = document.getElementById('content');
  content.appendChild(el('div', { class: 'loading', text: '正在加载日志…' }));
  try {
    state.logs.kind = kind;
    state.logs.page = page || 1;
    state.logs.data = await bridge.apiGet('logs/' + kind, query);
  } catch (error) {
    state.logs.data = null;
    toast('加载日志失败：' + error.message, 'bad');
  }
  await render();
}

async function viewLogs(root) {
  const kind = state.logs.kind;
  const data = state.logs.data;
  const filters = state.logs.filters || {};

  const tabs = el('div', { class: 'tabs' });
  LOG_TABS.forEach((tab) => {
    tabs.appendChild(el('button', {
      class: tab.kind === kind ? 'active' : '',
      text: tab.label,
      onclick: () => loadLogs(tab.kind, 1),
    }));
  });

  const groupInput = el('input', { type: 'text', placeholder: '按 group_openid 过滤', value: filters.group_id || '' });
  const keywordInput = el('input', { type: 'text', placeholder: '关键字（路径/昵称/片段）', value: filters.keyword || '' });
  const daysSelect = el('select');
  [1, 7, 30].forEach((days) => {
    daysSelect.appendChild(el('option', { value: String(days), text: '近 ' + days + ' 天', selected: String(filters.days) === String(days) ? 'selected' : null }));
  });
  const appealedSelect = el('select');
  [['', '申诉：全部'], ['1', '申诉：已申诉'], ['0', '申诉：未申诉']].forEach((pair) => {
    appealedSelect.appendChild(el('option', {
      value: pair[0],
      text: pair[1],
      selected: String(filters.appealed || '') === pair[0] ? 'selected' : null,
    }));
  });
  const applyBtn = el('button', { class: 'btn ghost', text: '筛选', onclick: () => {
    state.logs.filters = {
      group_id: groupInput.value.trim() || undefined,
      keyword: keywordInput.value.trim() || undefined,
      days: daysSelect.value,
      appealed: appealedSelect.value || undefined,
    };
    loadLogs(kind, 1);
  } });
  const clearBtn = el('button', { class: 'btn ghost', text: '清空筛选', onclick: () => {
    state.logs.filters = {};
    loadLogs(kind, 1);
  } });
  const exportBtn = el('button', { class: 'btn ghost', text: '导出 CSV', onclick: async () => {
    exportBtn.disabled = true;
    try {
      await bridge.download('logs/export', Object.assign({ kind, format: 'csv' }, state.logs.filters), kind + '.csv');
    } catch (error) { toast('导出失败：' + error.message, 'bad'); }
    finally { exportBtn.disabled = false; }
  } });

  clear(root);
  root.appendChild(card('日志中心', '审核事件与处置将在 M2 之后产生；API 调用与能力受限日志现在即可查看。', [
    tabs,
    el('div', { class: 'row' }, [groupInput, keywordInput, daysSelect, appealedSelect, el('div', { class: 'field-actions' }, [applyBtn, clearBtn, exportBtn])]),
  ]));

  if (!data) {
    root.appendChild(notice('暂无数据或加载失败。'));
    return;
  }

  const columns = logColumns(kind);
  const tbody = el('tbody');
  (data.items || []).forEach((row) => {
    tbody.appendChild(el('tr', {}, columns.map((column) => el('td', {
      text: cellValue(kind, column, row),
      class: column === 'note' || column === 'reason' ? 'mono' : '',
    }))));
  });
  if (!(data.items || []).length) {
    root.appendChild(notice('该筛选条件下没有记录。'));
    return;
  }

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 20)));
  const prev = el('button', { class: 'btn small ghost', text: '上一页', disabled: data.page <= 1 ? 'disabled' : null, onclick: () => loadLogs(kind, data.page - 1) });
  const next = el('button', { class: 'btn small ghost', text: '下一页', disabled: data.page >= totalPages ? 'disabled' : null, onclick: () => loadLogs(kind, data.page + 1) });

  root.appendChild(card('记录（共 ' + (data.total || 0) + ' 条）', null, [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, columns.map((column) => el('th', { text: column })))]),
        tbody,
      ]),
    ]),
    el('div', { class: 'pager' }, [prev, el('span', { text: '第 ' + data.page + ' / ' + totalPages + ' 页' }), next]),
  ]));

  const clearLogsBtn = el('button', { class: 'btn danger', text: '清空当前类型日志', onclick: async () => {
    if (!(await uiConfirm('将删除 ' + kind + ' 类型的全部日志，操作不可恢复。确认？'))) return;
    clearLogsBtn.disabled = true;
    try {
      const result = await bridge.apiPost('logs/clear', { scope: kind });
      toast('已删除 ' + JSON.stringify(result.deleted || {}), 'ok');
      loadLogs(kind, 1);
    } catch (error) { toast('清空失败：' + error.message, 'bad'); }
    finally { clearLogsBtn.disabled = false; }
  } });
  root.appendChild(card('危险操作', '清空后不可恢复；导出 CSV 可先留档。', [
    el('div', { class: 'field-actions' }, [clearLogsBtn]),
  ]));
}

/* ---------------------------------------------------------------- 工具页 */

async function viewTools(root) {
  const config = await loadConfig();
  clear(root);

  const selfcheckOut = el('pre', { class: 'guide', text: '尚未执行。' });
  const selfcheckBtn = el('button', { class: 'btn', text: '执行能力自检', onclick: async () => {
    selfcheckBtn.disabled = true;
    selfcheckOut.textContent = '正在探测平台能力…';
    try {
      const result = await bridge.apiPost('selfcheck', {});
      const lines = [];
      Object.keys(result.report || {}).forEach((groupId) => {
        const item = result.report[groupId];
        lines.push('群 ' + shortId(groupId));
        Object.keys(item.capabilities || {}).forEach((cap) => {
          const record = item.capabilities[cap];
          const flag = record.probed === false ? '➖' : (record.ok ? '✅' : '❌');
          let line = '  ' + flag + ' ' + cap;
          if (record.err_code) line += ' err_code=' + record.err_code;
          if (record.note) line += ' · ' + record.note;
          lines.push(line);
        });
        (item.suggestions || []).forEach((text) => lines.push('  👉 ' + text));
        lines.push('');
      });
      lines.push('平台通道：' + (result.transport && result.transport.available ? '可用' : '不可用'));
      selfcheckOut.textContent = lines.join('\n') || '没有可自检的群。';
    } catch (error) {
      selfcheckOut.textContent = '自检失败：' + error.message;
    } finally { selfcheckBtn.disabled = false; }
  } });

  root.appendChild(card('能力自检', '逐项调用平台只读接口。受限（err_code=11253）表示该接口未对本机器人开放。', [
    el('div', { class: 'field-actions' }, [selfcheckBtn]),
    selfcheckOut,
  ]));

  let dbInfo = null;
  try { dbInfo = await bridge.apiGet('db/info'); } catch (error) { dbInfo = null; }
  const dbOut = el('div', { class: 'grid cols-2' });
  if (dbInfo && dbInfo.db) {
    dbOut.appendChild(el('div', { class: 'notice', text: '数据库：' + dbInfo.db.path }));
    dbOut.appendChild(el('div', { class: 'notice', text: '文件大小：' + Math.round((dbInfo.db.size_bytes || 0) / 1024) + ' KB（WAL ' + Math.round((dbInfo.db.wal_bytes || 0) / 1024) + ' KB）' }));
    Object.keys(dbInfo.db.tables || {}).forEach((key) => {
      const table = dbInfo.db.tables[key];
      dbOut.appendChild(el('div', { class: 'notice', text: table.table + '：' + table.count + ' 行' + (table.oldest ? '（' + fmtTime(table.oldest) + ' 起）' : '') }));
    });
    dbOut.appendChild(el('div', { class: 'notice', text: '写队列：' + JSON.stringify(dbInfo.queue || {}) }));
  } else {
    dbOut.appendChild(notice('无法读取数据库信息。', 'bad'));
  }

  const mkDbBtn = (label, op, confirmText) => el('button', { class: 'btn ghost', text: label, onclick: async () => {
    if (confirmText && !(await uiConfirm(confirmText))) return;
    try {
      await bridge.apiPost('db/maintain', { op });
      toast('已执行：' + op, 'ok');
      await render();
    } catch (error) { toast(op + ' 失败：' + error.message, 'bad'); }
  } });

  root.appendChild(card('审计库维护', '按保留策略裁剪历史、整理文件体积；备份会直接下载一份一致性副本。', [
    dbOut,
    el('div', { class: 'field-actions' }, [
      mkDbBtn('按保留策略裁剪', 'prune', null),
      mkDbBtn('VACUUM 整理', 'vacuum', '整理期间数据库会短暂锁定，确认执行？'),
      el('button', { class: 'btn ghost', text: '备份并下载', onclick: async () => {
        try { await bridge.download('db/maintain', { op: 'backup' }, 'moderation-backup.db'); }
        catch (error) { toast('备份失败：' + error.message, 'bad'); }
      } }),
    ]),
  ]));

  const sseBox = el('pre', { class: 'guide', text: '实时事件将显示在这里（默认订阅 audit）…' });
  const sseBtn = el('button', { class: 'btn ghost', text: '开始实时订阅', onclick: async () => {
    if (state.sse) {
      try { await bridge.unsubscribeSSE(state.sse); } catch (ignore) { /* 已断开 */ }
      state.sse = null;
      sseBtn.textContent = '开始实时订阅';
      return;
    }
    try {
      state.sse = await bridge.subscribeSSE('events/stream', {
        onMessage: (event) => {
          state.sseLines.unshift(fmtTime(new Date().toISOString()) + ' ' + (event.raw || ''));
          state.sseLines = state.sseLines.slice(0, 60);
          sseBox.textContent = state.sseLines.join('\n');
        },
      }, { topic: 'audit' });
      sseBtn.textContent = '停止订阅';
    } catch (error) { toast('订阅失败：' + error.message, 'bad'); }
  } });

  root.appendChild(card('实时事件（SSE）', '用于验证 WebUI ↔ 后端通道；审核事件在 M2 接入后会大量出现。', [
    el('div', { class: 'field-actions' }, [sseBtn]),
    sseBox,
  ]));

  let instructions = null;
  try { instructions = await bridge.apiGet('instructions'); } catch (error) { instructions = null; }
  if (instructions) {
    const list = el('div', { class: 'grid cols-2' });
    [['public', '所有人'], ['group_admin', '群主 / 群管理员'], ['admin', 'AstrBot 管理员']].forEach((pair) => {
      const items = instructions[pair[0]] || [];
      if (!items.length) return;
      const box = el('div', { class: 'notice' });
      box.appendChild(el('div', { text: '【' + pair[1] + '】' }));
      items.forEach((item) => box.appendChild(el('div', { class: 'mono', text: item.command + ' — ' + item.desc })));
      list.appendChild(box);
    });
    root.appendChild(card('指令速查', instructions.note || '', [list]));
  }

  root.appendChild(card('当前配置摘要', '完整编辑在「策略 / 关键词」视图（M2 提供）。', [
    el('pre', { class: 'guide', text: JSON.stringify(config.settings || {}, null, 2) }),
  ]));
}

/* ----------------------------------------------------------- 策略视图 */

const MATRIX_ACTIONS = ['warn', 'recall', 'mute', 'report', 'blacklist', 'remove'];
const RULE_TYPES = ['normalized', 'literal', 'regex', 'fuzzy', 'pinyin'];
const ACTION_LABELS = {
  warn: '警告',
  recall: '撤回',
  mute: '禁言',
  report: '上报',
  blacklist: '拉黑',
  remove: '移除',
};
const CONDITION_LABELS = {
  rule_hit: '规则命中',
  has_link: '含链接',
  has_contact: '疑似联系方式',
  ad_template: '命中广告模板',
  has_image: '含图片',
  long_text: '长文本',
  new_member: '新成员',
  flood: '刷屏',
  all: '全部消息',
};

const RULE_TYPE_LABELS = {
  normalized: '关键词(归一化)',
  literal: '精确关键词',
  regex: '正则',
  fuzzy: '模糊匹配',
  pinyin: '同音匹配',
  template: '模板',
};

function numField(label, value, min, max, step) {
  const input = el('input', {
    type: 'number',
    value: String(value),
    min: String(min),
    max: String(max),
    step: String(step || 1),
  });
  return { node: el('label', { class: 'field' }, [el('span', { text: label }), input]), input };
}

function checkField(label, value) {
  const input = el('input', { type: 'checkbox' });
  input.checked = !!value;
  return { node: el('label', { class: 'field' }, [el('span', { text: label }), input]), input };
}

async function viewPolicy(root) {
  const config = await loadConfig();
  const settings = Object.assign({}, config.settings || {});
  const options = config.options || {};
  clear(root);

  /* 运行参数 */
  const dryRun = checkField('dry-run（不做撤回/禁言等实际处置，仍会发警告）', settings.dry_run);
  const dryRunWarn = checkField('dry-run 期间仍然发送警告与上报', settings.dry_run_warn !== false);
  const allowNoFull = checkField('允许未开启「接收全部消息」时启用审核（不建议）', settings.allow_without_full_msg);
  const blockLlm = checkField('违规消息阻断后续 LLM 对话', settings.block_llm_on_violation);
  const repeatMul = checkField('重复违规时长累加（≤3 倍）', settings.repeat_offense_multiplier);
  const autoBlacklist = checkField('自动拉黑（内邀能力）', settings.auto_blacklist);
  const autoRemove = checkField('自动移除成员（内邀能力，风险高）', settings.auto_remove);
  const modeSelect = el('select');
  (options.modes || []).forEach((mode) => {
    modeSelect.appendChild(el('option', { value: mode, text: mode, selected: mode === settings.mode ? 'selected' : null }));
  });
  const imageReview = el('select');
  [['off', '图片不送审（默认）'], ['with_text', '仅图文混排时把图一起送审'], ['always', '纯图片也送审']]
    .forEach((pair) => {
      imageReview.appendChild(el('option', {
        value: pair[0],
        text: pair[1],
        selected: (settings.image_review || 'off') === pair[0] ? 'selected' : null,
      }));
    });
  const imageMax = numField('每条消息图片送审上限', settings.image_review_max || 1, 1, 4, 1);
  const imageHint = el('p', {
    class: 'card-desc',
    text: '图片以多模态方式交给同一个审核模型（token 开销更高）；语音按平台转写文本审核；视频/文件只记类型。',
  });
  const providerInfo = config.providers || {};
  const providerSelect = el('select');
  providerSelect.appendChild(el('option', { value: '', text: '跟随会话默认模型' }));
  (providerInfo.items || []).forEach((item) => {
    const label = (item.model || item.id) + (item.type ? '（' + item.type + '）' : '');
    providerSelect.appendChild(el('option', {
      value: item.id,
      text: label,
      selected: item.id === settings.llm_provider_id ? 'selected' : null,
    }));
  });
  const providerHint = el('p', {
    class: 'card-desc',
    text: '当前生效的审核模型：' + (providerInfo.last_used || providerInfo.configured || '跟随会话默认模型')
      + '；选择「跟随会话默认模型」时，审核会使用 AstrBot 中该会话选定的对话模型。',
  });
  const minConf = numField('LLM 置信度门槛', settings.llm_min_confidence, 0, 1, 0.05);
  const sampleRate = numField('送审采样率', settings.sample_rate, 0, 1, 0.05);
  const timeoutField = numField('单次超时（秒）', settings.llm_timeout, 5, 120, 1);
  const qpmField = numField('单群 QPM', settings.llm_qpm_per_group, 1, 120, 1);
  const concurrency = numField('全局并发', settings.llm_max_concurrency, 1, 16, 1);
  const budget = numField('每日预算（0=不限）', settings.llm_daily_budget, 0, 100000, 10);
  const cacheTtl = numField('结果缓存（秒）', settings.cache_ttl, 0, 86400, 30);
  const breaker = numField('熔断阈值（连续失败）', settings.circuit_break_threshold, 1, 50, 1);
  const maxMuteDays = numField('最长禁言（天）', settings.max_mute_days, 1, 30, 1);
  const notifySession = el('input', { type: 'text', value: settings.notify_session || '', placeholder: '如 爱莉希雅:GROUP_MESSAGE:xxxx' });

  const conditions = el('div', { class: 'row' });
  Object.keys(CONDITION_LABELS).forEach((key) => {
    const box = el('input', { type: 'checkbox' });
    box.checked = (settings.send_conditions || []).indexOf(key) >= 0;
    box.dataset.key = key;
    conditions.appendChild(el('label', { class: 'switch' }, [box, el('span', { text: CONDITION_LABELS[key] })]));
  });
  const riskBox = el('input', { type: 'checkbox' });
  riskBox.checked = (settings.send_conditions || []).some((item) => String(item).indexOf('risk>=') === 0);
  riskBox.dataset.key = '__risk__';
  conditions.appendChild(el('label', { class: 'switch' }, [riskBox, el('span', { text: '本地风险分达到阈值' })]));
  const riskField = numField('风险分阈值（达到即送审）', settings.risk_send_threshold || 60, 10, 100, 5);

  /* 规则增强参数 */
  const normalizeOn = checkField('启用文本归一化（识别形近字/插符号/全角变体）', settings.normalize_enabled !== false);
  const homoglyphOn = checkField('启用形近字表', settings.homoglyph_enabled !== false);
  const templateOn = checkField('启用广告模板（动作词 × 诱饵词）', settings.template_enabled !== false);
  const pinyinOn = checkField('启用同音匹配（需已安装 pypinyin）', settings.pinyin_enabled);
  const fuzzyDist = numField('模糊匹配编辑距离（0=关闭）', settings.fuzzy_max_distance, 0, 3, 1);
  const autoEnforce = checkField('归一化/模板命中直接按规则动作处置（默认关：先送审）', settings.auto_enforce_normalized);
  const dupWindow = numField('同文案多号刷屏窗口（秒）', settings.duplicate_flood_window || 300, 30, 3600, 30);
  const dupMembers = numField('同文案多号刷屏人数阈值', settings.duplicate_flood_members || 3, 2, 20, 1);
  const domainAllow = checkField('竞赛域名白名单降权（白名单内链接不计 link 分，仍走规则与送审）', settings.domain_allowlist_enabled !== false);
  const domainArea = el('textarea', { rows: '4', class: 'mono', placeholder: '一行一个域名，例如 codeforces.com' });
  domainArea.value = (settings.domain_allowlist || []).join('\n');
  const appealEnabled = checkField('接受成员申诉（申诉 <理由>）', settings.appeal_enabled !== false);
  const appealAuto = checkField('申诉通过自动加入误判白名单（默认关，可能被社工利用）', settings.appeal_auto_whitelist);
  const appealNotify = checkField('申诉处理结果回执申诉人', settings.appeal_notify !== false);
  const pinyinHint = el('p', {
    class: 'card-desc',
    text: '同音匹配依赖可选的 pypinyin 库；未安装时该项自动失效（不影响其他规则）。',
  });

  /* 处置矩阵 */
  const matrix = JSON.parse(JSON.stringify(settings.action_matrix || { violation: {} }));
  const matrixBox = el('div', { class: 'grid cols-2' });
  const matrixInputs = {};
  for (let severity = 1; severity <= 5; severity += 1) {
    const key = String(severity);
    const current = (matrix.violation && (matrix.violation[key] || matrix.violation[severity])) || [];
    const row = el('div', { class: 'notice' });
    row.appendChild(el('div', { text: 'severity = ' + key }));
    const wrap = el('div', { class: 'row' });
    const list = Array.isArray(current) ? current : [current];
    matrixInputs[key] = {};
    MATRIX_ACTIONS.forEach((action) => {
      const box = el('input', { type: 'checkbox' });
      box.checked = list.indexOf(action) >= 0;
      matrixInputs[key][action] = box;
      wrap.appendChild(el('label', { class: 'switch' }, [box, el('span', { text: ACTION_LABELS[action] })]));
    });
    row.appendChild(wrap);
    matrixBox.appendChild(row);
  }

  /* 提示词 */
  const systemPrompt = el('textarea', { placeholder: '留空使用内置默认提示词' });
  systemPrompt.value = settings.prompt_system || '';
  const userPrompt = el('textarea', { placeholder: '留空使用内置默认模板' });
  userPrompt.value = settings.prompt_user || '';
  const promptHint = el('p', {
    class: 'card-desc',
    text: '可用占位符：{rules_brief} {rule_summary} {message_kind} {sender_name} {sender_role} {days} {recent} {text}',
  });

  const saveBtn = el('button', { class: 'btn', text: '保存策略', onclick: async () => {
    saveBtn.disabled = true;
    const matrixPayload = {};
    Object.keys(matrixInputs).forEach((severity) => {
      const picked = MATRIX_ACTIONS.filter((action) => matrixInputs[severity][action].checked);
      matrixPayload[severity] = picked;
    });
    const payload = {
      dry_run: dryRun.input.checked,
      dry_run_warn: dryRunWarn.input.checked,
      allow_without_full_msg: allowNoFull.input.checked,
      block_llm_on_violation: blockLlm.input.checked,
      repeat_offense_multiplier: repeatMul.input.checked,
      auto_blacklist: autoBlacklist.input.checked,
      auto_remove: autoRemove.input.checked,
      mode: modeSelect.value,
      llm_provider_id: providerSelect.value,
      image_review: imageReview.value,
      image_review_max: Number(imageMax.input.value),
      llm_min_confidence: Number(minConf.input.value),
      sample_rate: Number(sampleRate.input.value),
      llm_timeout: Number(timeoutField.input.value),
      llm_qpm_per_group: Number(qpmField.input.value),
      llm_max_concurrency: Number(concurrency.input.value),
      llm_daily_budget: Number(budget.input.value),
      cache_ttl: Number(cacheTtl.input.value),
      circuit_break_threshold: Number(breaker.input.value),
      max_mute_days: Number(maxMuteDays.input.value),
      notify_session: notifySession.value.trim(),
      action_matrix: { violation: matrixPayload },
      send_conditions: Array.from(conditions.querySelectorAll('input'))
        .filter((box) => box.checked && box.dataset.key !== '__risk__')
        .map((box) => box.dataset.key)
        .concat(riskBox.checked ? ['risk>=' + String(riskField.input.value)] : []),
      risk_send_threshold: Number(riskField.input.value),
      normalize_enabled: normalizeOn.input.checked,
      homoglyph_enabled: homoglyphOn.input.checked,
      template_enabled: templateOn.input.checked,
      pinyin_enabled: pinyinOn.input.checked,
      fuzzy_max_distance: Number(fuzzyDist.input.value),
      auto_enforce_normalized: autoEnforce.input.checked,
      duplicate_flood_window: Number(dupWindow.input.value),
      duplicate_flood_members: Number(dupMembers.input.value),
      domain_allowlist_enabled: domainAllow.input.checked,
      domain_allowlist: domainArea.value.split('\n').map((item) => item.trim()).filter(Boolean),
      appeal_enabled: appealEnabled.input.checked,
      appeal_auto_whitelist: appealAuto.input.checked,
      appeal_notify: appealNotify.input.checked,
      prompt_system: systemPrompt.value,
      prompt_user: userPrompt.value,
    };
    try {
      await bridge.apiPost('config', { section: 'settings', data: payload });
      state.config = null;
      toast('策略已保存（送审条件：' + (payload.send_conditions.join('、') || '无') + '）', 'ok');
      await render();
    } catch (error) {
      toast('保存失败：' + error.message, 'bad');
    } finally { saveBtn.disabled = false; }
  } });

  /* 试跑 */
  const dryText = el('textarea', { placeholder: '粘贴一段消息，点「试跑」查看完整判定链（不会执行任何动作）' });
  const dryOut = el('pre', { class: 'guide', text: '尚未试跑。' });
  const dryBtn = el('button', { class: 'btn ghost', text: '试跑', onclick: async () => {
    dryBtn.disabled = true;
    dryOut.textContent = '正在判定…';
    try {
      const result = await bridge.apiPost('dryrun', { kind: 'message', text: dryText.value });
      dryOut.textContent = JSON.stringify(result, null, 2);
    } catch (error) {
      dryOut.textContent = '试跑失败：' + error.message;
    } finally { dryBtn.disabled = false; }
  } });

  root.appendChild(card('规则增强（变体识别 / 风险分）',
    '归一化让规则"看得见"形近字、插符号、全角与拼音变体；风险分把多个弱信号累加，达到阈值即送审（不影响纯闲聊）。', [
    el('div', { class: 'row' }, [normalizeOn.node, homoglyphOn.node, templateOn.node, pinyinOn.node]),
    el('div', { class: 'row' }, [fuzzyDist.node, riskField.node, dupWindow.node, dupMembers.node]),
    el('div', { class: 'row' }, [autoEnforce.node]),
    pinyinHint,
  ]));

  root.appendChild(card('竞赛域名白名单', '白名单内的链接不计 link 分（25 分），但**只降权不豁免**：规则、审计与 LLM 送审判断照常执行。', [
    el('div', { class: 'row' }, [domainAllow.node]),
    domainArea,
    el('p', { class: 'card-desc', text: '一行一个域名，按点边界后缀匹配：codeforces.com 命中 m1.codeforces.com，但不命中 fake-codeforces.com。' }),
  ]));

  root.appendChild(card('申诉闭环', '成员回复被处置消息发送「申诉 <理由>」；群管回复申诉消息发送「申诉通过 / 申诉驳回」。误判白名单默认关闭。', [
    el('div', { class: 'row' }, [appealEnabled.node, appealNotify.node, appealAuto.node]),
    el('p', { class: 'card-desc', text: '申诉通过会自动解禁（若该事件产生过禁言）并追加 appeal_accepted 动作留痕，不删除原审核事件。' }),
  ]));

  root.appendChild(card('运行参数', '首次安装默认 dry-run + lenient；确认判定质量后再关闭 dry-run 并切到标准档。', [
    el('div', { class: 'row' }, [dryRun.node, dryRunWarn.node, allowNoFull.node, blockLlm.node]),
    el('div', { class: 'row' }, [
      el('label', { class: 'field' }, [el('span', { text: '默认模式' }), modeSelect]),
      el('label', { class: 'field' }, [el('span', { text: '审核模型' }), providerSelect]),
      el('label', { class: 'field' }, [el('span', { text: '图片审核' }), imageReview]),
      minConf.node, sampleRate.node, timeoutField.node,
    ]),
    el('div', { class: 'row' }, [imageMax.node]),
    imageHint,
    el('div', { class: 'row' }, [qpmField.node, concurrency.node, budget.node, cacheTtl.node, breaker.node]),
    el('div', { class: 'row' }, [maxMuteDays.node, repeatMul.node, autoBlacklist.node, autoRemove.node]),
    providerHint,
    el('label', { class: 'field' }, [el('span', { text: '管理员通知会话（umo）' }), notifySession]),
    el('div', { class: 'field-actions' }, [el('span', { class: 'muted', text: '送审条件：' })]),
    conditions,
  ]));

  root.appendChild(card('处置矩阵', 'verdict = violation 时按 severity 执行的动作；lenient 模式只会保留警告与上报。', [matrixBox]));

  root.appendChild(card('提示词', '留空使用内置默认值；修改后建议先在下方试跑验证。', [promptHint, systemPrompt, userPrompt]));

  root.appendChild(card('保存', null, [el('div', { class: 'field-actions' }, [saveBtn])]));

  root.appendChild(card('试跑（dry-run）', '完整走一遍「规则 → LLM → 动作规划」，不执行任何真实动作。', [
    dryText,
    el('div', { class: 'field-actions' }, [dryBtn]),
    dryOut,
  ]));
}

/* --------------------------------------------------------- 关键词视图 */

async function viewKeywords(root) {
  const config = await loadConfig();
  const keywords = JSON.parse(JSON.stringify(config.keywords || { hard: [], soft: [] }));
  clear(root);

  const groups = config.groups || [];
  const groupSelect = el('select');
  groupSelect.appendChild(el('option', { value: '', text: '（全局，作用于所有群）' }));
  groups.forEach((group) => {
    groupSelect.appendChild(el('option', { value: group.group_id, text: (group.name || shortId(group.group_id)) }));
  });

  const typeSelect = el('select');
  [['normalized', '关键词（推荐：自动识别形近字/插符号变体）'],
   ['literal', '精确关键词（只匹配原样文字）'],
   ['regex', '正则表达式'],
   ['fuzzy', '模糊匹配（允许少量错别字）'],
   ['pinyin', '同音匹配（需已安装 pypinyin）']].forEach((pair) => {
    typeSelect.appendChild(el('option', { value: pair[0], text: pair[1] }));
  });
  const bucketSelect = el('select');
  [['hard', '硬规则（命中即处置）'], ['soft', '软规则（提升关注，仍由 LLM 判定）']].forEach((pair) => {
    bucketSelect.appendChild(el('option', { value: pair[0], text: pair[1] }));
  });
  const patternInput = el('input', { type: 'text', placeholder: '例如：加群 / 私\\s*聊 / https?://' });
  const actionWrap = el('div', { class: 'row' });
  const actionBoxes = {};
  MATRIX_ACTIONS.forEach((action) => {
    const box = el('input', { type: 'checkbox' });
    // 硬规则的常见诉求是"撤回+禁言"，默认勾选这两个，避免新建规则后只警告
    box.checked = action === 'recall' || action === 'mute';
    actionBoxes[action] = box;
    actionWrap.appendChild(el('label', { class: 'switch' }, [box, el('span', { text: ACTION_LABELS[action] })]));
  });

  const reload = async (next) => {
    try {
      await bridge.apiPost('config', { section: 'keywords', data: next });
      state.config = null;
      toast('规则库已保存', 'ok');
      await render();
    } catch (error) { toast('保存失败：' + error.message, 'bad'); }
  };

  const addBtn = el('button', { class: 'btn', text: '添加规则', onclick: async () => {
    const pattern = patternInput.value.trim();
    if (!pattern) { toast('请填写规则内容', 'bad'); return; }
    const bucket = bucketSelect.value;
    const actions = MATRIX_ACTIONS.filter((action) => actionBoxes[action].checked);
    if (bucket === 'hard' && !actions.length) { toast('硬规则至少要选一个动作', 'bad'); return; }
    const next = JSON.parse(JSON.stringify(keywords));
    next[bucket] = next[bucket] || [];
    next[bucket].push({
      id: pattern.slice(0, 24),
      type: typeSelect.value,
      pattern,
      action: bucket === 'hard' ? actions : [],
      scope: groupSelect.value || 'all',
      enabled: true,
      note: '',
    });
    await reload(next);
  } });

  const testText = el('textarea', { placeholder: '输入一段测试文本，查看命中的规则' });
  const testOut = el('pre', { class: 'guide', text: '尚未测试。' });
  const testBtn = el('button', { class: 'btn ghost', text: '命中测试', onclick: async () => {
    testBtn.disabled = true;
    try {
      const result = await bridge.apiPost('rules/test', {
        text: testText.value,
        group_id: groupSelect.value || '',
      });
      testOut.textContent = JSON.stringify(result, null, 2);
    } catch (error) { testOut.textContent = '测试失败：' + error.message; }
    finally { testBtn.disabled = false; }
  } });

  root.appendChild(card('添加规则', '硬规则命中即按【规则自身所选动作】处置（不再叠加处置矩阵）；软规则只提升关注度，最终仍由 LLM 判定。', [
    el('div', { class: 'row' }, [bucketSelect, typeSelect, patternInput, groupSelect]),
    actionWrap,
    el('div', { class: 'field-actions' }, [addBtn]),
  ]));

  for (const bucket of ['hard', 'soft']) {
    const items = keywords[bucket] || [];
    const tbody = el('tbody');
    items.forEach((item, index) => {
      const toggle = el('input', { type: 'checkbox' });
      toggle.checked = item.enabled !== false;
      toggle.addEventListener('change', async () => {
        const next = JSON.parse(JSON.stringify(keywords));
        next[bucket][index].enabled = toggle.checked;
        await reload(next);
      });
      const del = el('button', { class: 'btn small danger', text: '删除', onclick: async () => {
        const next = JSON.parse(JSON.stringify(keywords));
        next[bucket].splice(index, 1);
        await reload(next);
      } });
      tbody.appendChild(el('tr', {}, [
        el('td', {}, [toggle]),
        el('td', { text: RULE_TYPE_LABELS[item.type || 'literal'] || (item.type || 'literal') }),
        el('td', { class: 'mono', text: item.pattern }),
        el('td', { text: (item.action || []).map((action) => ACTION_LABELS[action] || action).join('、') || '—' }),
        el('td', { text: String(item.scope || 'all') === 'all' ? '全局' : shortId(item.scope) }),
        el('td', {}, [del]),
      ]));
    });
    root.appendChild(card(bucket === 'hard' ? '硬规则' : '软规则', '共 ' + items.length + ' 条', [
      el('div', { class: 'table-wrap' }, [
        el('table', {}, [
          el('thead', {}, [el('tr', {}, ['启用', '类型', '内容', '动作', '作用域', '操作'].map((text) => el('th', { text })))]),
          tbody,
        ]),
      ]),
    ]));
  }

  root.appendChild(card('命中测试', '与真实审核使用同一套规则引擎（含外链、联系方式、刷屏等内置检测）。', [
    testText,
    el('div', { class: 'field-actions' }, [testBtn]),
    testOut,
  ]));
}

/* ----------------------------------------------------- 规则增强视图 */

async function viewRulesX(root) {
  const config = await loadConfig();
  const templates = JSON.parse(JSON.stringify(config.templates || []));
  const homoglyph = Object.assign({}, config.homoglyph || {});
  clear(root);

  root.appendChild(notice('这里维护"变体识别"的两块数据：广告模板（动作词 × 诱饵词）与形近字表；'
    + '规则引擎保存后立即生效（无需重载插件）。', 'ok'));

  /* 命中测试 */
  const testText = el('input', { type: 'text', placeholder: '输入消息测试判定，例如：珈裙苓资料123456' });
  const testOut = el('pre', { class: 'mono out' });
  const testBtn = el('button', { class: 'btn', text: '测试判定', onclick: async () => {
    testBtn.disabled = true;
    try {
      const result = await bridge.apiPost('rules/test', { text: testText.value });
      const lines = [];
      lines.push('是否送审 LLM：' + (result.should_send ? '是' : '否'));
      lines.push('本地风险分：' + String(result.score) + ' / 100');
      const views = result.views || {};
      lines.push('原文 view    ：' + (views.raw || ''));
      lines.push('compact 视图 ：' + (views.compact || ''));
      lines.push('骨架视图     ：' + (views.skeleton || ''));
      if (views.pinyin) lines.push('拼音视图     ：' + views.pinyin);
      const signals = result.signals || {};
      lines.push('风险信号     ：' + (Object.keys(signals).length
        ? Object.keys(signals).map((key) => key + '(+' + signals[key] + ')').join('、') : '无'));
      (result.hits || []).forEach((hit) => {
        lines.push('命中：[' + (RULE_TYPE_LABELS[hit.rule_type] || hit.rule_type) + '] '
          + hit.pattern + ' → 动作 ' + ((hit.actions || []).join('、') || '无')
          + (hit.enforce ? '（直接处置）' : '（仅送审）'));
      });
      if (!(result.hits || []).length) lines.push('命中：无');
      testOut.textContent = lines.join('\n');
    } catch (error) { testOut.textContent = '测试失败：' + (error && error.message ? error.message : error); }
    finally { testBtn.disabled = false; }
  } });

  root.appendChild(card('判定测试', '展示三个归一化视图、命中依据与风险分明细，用来判断某条消息为什么被/不被送审。', [
    el('div', { class: 'row' }, [testText, el('div', { class: 'field-actions' }, [testBtn])]),
    testOut,
  ]));

  /* 广告模板 */
  const tplArea = el('textarea', { rows: '14', class: 'mono' });
  tplArea.value = JSON.stringify(templates, null, 2);
  const tplStatus = el('p', { class: 'card-desc', text: templates.length
    ? '当前有 ' + templates.length + ' 条自定义模板；留空数组 [] 表示关闭模板规则（回退到仅规则匹配）。'
    : '当前未自定义模板：引擎使用内置模板（拉群引流 / 私聊引流 / 兼职刷单 / 赌博引流 / 涉黄引流）。' });
  const tplSave = el('button', { class: 'btn', text: '保存模板', onclick: async () => {
    tplSave.disabled = true;
    try {
      const parsed = JSON.parse(tplArea.value || '[]');
      if (!Array.isArray(parsed)) throw new Error('模板必须是数组');
      await bridge.apiPost('config', { section: 'templates', data: parsed });
      state.config = null;
      toast('模板已保存并生效', 'ok');
      await render();
    } catch (error) {
      toast('保存失败：' + (error && error.message ? error.message : error), 'bad');
    } finally { tplSave.disabled = false; }
  } });
  const tplReset = el('button', { class: 'btn ghost', text: '恢复内置模板', onclick: async () => {
    try {
      await bridge.apiPost('config', { section: 'templates', data: [] });
      state.config = null;
      toast('已恢复为内置模板', 'ok');
      await render();
    } catch (error) { toast('操作失败：' + (error && error.message ? error.message : error), 'bad'); }
  } });
  root.appendChild(card('广告模板', '结构式规则：每个 all_of 分组里任一 any_of 命中即该组成立，全部分组成立即命中模板。'
    + '模板命中只产生风险分并送审，是否处置由 LLM 判定结果决定。', [
    tplArea,
    tplStatus,
    el('div', { class: 'field-actions' }, [tplSave, tplReset]),
  ]));

  /* 形近字表 */
  const lines = Object.keys(homoglyph).map((key) => key + '=' + homoglyph[key]);
  const hgArea = el('textarea', { rows: '8', class: 'mono' });
  hgArea.value = lines.join('\n');
  hgArea.placeholder = '每行一条，形近字=标准字，例如：珈=加';
  const hgSave = el('button', { class: 'btn', text: '保存形近字表', onclick: async () => {
    hgSave.disabled = true;
    try {
      const payload = {};
      (hgArea.value || '').split('\n').forEach((line) => {
        const parts = line.split('=');
        if (parts.length === 2 && parts[0].trim() && parts[1].trim()) {
          payload[parts[0].trim()] = parts[1].trim();
        }
      });
      await bridge.apiPost('config', { section: 'homoglyph', data: payload });
      state.config = null;
      toast('形近字表已保存（' + Object.keys(payload).length + ' 条）', 'ok');
      await render();
    } catch (error) {
      toast('保存失败：' + (error && error.message ? error.message : error), 'bad');
    } finally { hgSave.disabled = false; }
  } });
  root.appendChild(card('形近字表', '留空使用内置基线（内置覆盖广告高频字：珈/裙/苓/咨/廖/薇/薪 等）。'
    + '在这里补充的条目会与内置表合并。', [
    hgArea,
    el('div', { class: 'field-actions' }, [hgSave]),
  ]));

  /* 误判白名单（申诉通过自动加白） */
  const wlBox = el('div', { class: 'loading', text: '正在加载申诉白名单…' });
  const adviceBox = el('p', { class: 'card-desc', text: '正在统计被申诉通过的规则…' });
  const loadWhitelist = async () => {
    clear(wlBox);
    try {
      const data = await bridge.apiGet('appeal_whitelist');
      const items = data.items || [];
      if (!items.length) {
        wlBox.appendChild(notice('当前没有误判白名单条目。'));
        return;
      }
      const tbody = el('tbody');
      items.forEach((row) => {
        const revoke = el('button', { class: 'btn small danger', text: '撤销', onclick: async () => {
          try {
            await bridge.apiPost('appeal_whitelist', { op: 'del', digest: row.digest });
            toast('已撤销', 'ok');
            await loadWhitelist();
          } catch (error) { toast('撤销失败：' + error.message, 'bad'); }
        } });
        tbody.appendChild(el('tr', {}, [
          el('td', { class: 'mono', text: String(row.digest || '').slice(0, 24) }),
          el('td', { text: (row.skeleton || '').slice(0, 40) }),
          el('td', { text: row.reason || '-' }),
          el('td', { text: row.added_by || '-' }),
          el('td', { text: fmtTime(row.added_at ? new Date(row.added_at * 1000).toISOString() : '') }),
          el('td', {}, [revoke]),
        ]));
      });
      wlBox.appendChild(el('div', { class: 'table-wrap' }, [el('table', {}, [
        el('thead', {}, [el('tr', {}, ['摘要', '骨架', '原因', '加入者', '时间', '操作'].map((t) => el('th', { text: t })))]),
        tbody,
      ])]));
    } catch (error) { wlBox.appendChild(notice('加载失败：' + error.message, 'bad')); }
  };
  const loadAdvice = async () => {
    try {
      const data = await bridge.apiGet('appeals', { state: 'accepted', days: 30, limit: 500 });
      const counts = {};
      (data.items || []).forEach((row) => {
        const key = row.category || '未分类';
        counts[key] = (counts[key] || 0) + 1;
      });
      const top = Object.keys(counts).sort((a, b) => counts[b] - counts[a]).slice(0, 5);
      adviceBox.textContent = top.length
        ? '被申诉通过最多（建议复查规则）：' + top.map((key) => key + ' ' + counts[key] + ' 次').join('、')
        : '近 30 天没有被申诉通过的记录。';
    } catch (error) { adviceBox.textContent = '统计失败：' + error.message; }
  };
  root.appendChild(card('误判白名单（申诉通过自动加白）',
    '只有 appeal_auto_whitelist 打开时才会写入；命中白名单的消息跳过 LLM 送审，但仍写审计（verdict=allow / category=whitelisted）。', [
    wlBox,
    adviceBox,
  ]));
  await loadWhitelist();
  await loadAdvice();
}

/* ----------------------------------------------------- 成员与禁言视图 */

async function viewMembers(root) {
  const config = await loadConfig();
  const groups = config.groups || [];
  clear(root);
  if (!groups.length) {
    root.appendChild(notice('还没有登记任何群：让机器人在群里收到一条消息后再回到这里。'));
    return;
  }
  const selected = (config.ui_state || {}).members_group || groups[0].group_id;
  const groupSelect = el('select');
  groups.forEach((group) => {
    groupSelect.appendChild(el('option', {
      value: group.group_id,
      text: (group.name || shortId(group.group_id)),
      selected: group.group_id === selected ? 'selected' : null,
    }));
  });
  groupSelect.addEventListener('change', async () => {
    await bridge.apiPost('ui_state', { members_group: groupSelect.value });
    state.config = null;
    await render();
  });

  const mutesBox = el('div', { class: 'loading', text: '正在加载禁言台账…' });
  const globalBlacklistBox = el('div', { class: 'loading', text: '正在加载跨群黑名单…' });
  const blacklistBox = el('div', { class: 'loading', text: '正在加载黑名单…' });
  const searchInput = el('input', { type: 'text', placeholder: '昵称关键字或完整 openid' });
  const searchOut = el('pre', { class: 'guide', text: '尚未查询。' });

  root.appendChild(card('成员与禁言', '禁言台账来自本地记录并与平台对账；黑名单区分「平台」与「本地」两套。', [
    el('div', { class: 'row' }, [groupSelect]),
  ]));

  const loadMutes = async () => {
    clear(mutesBox);
    try {
      const data = await bridge.apiGet('mutes', { group_id: groupSelect.value });
      const items = data.items || [];
      if (!items.length) {
        mutesBox.appendChild(notice('当前没有生效中的禁言记录。'));
        return;
      }
      const tbody = el('tbody');
      items.forEach((row) => {
        const unmute = el('button', { class: 'btn small ghost', text: '解禁', onclick: async () => {
          try {
            await bridge.apiPost('mutes/unmute', {
              group_id: groupSelect.value,
              member_openids: [row.member_openid],
            });
            toast('已解禁', 'ok');
            await loadMutes();
          } catch (error) { toast('解禁失败：' + error.message, 'bad'); }
        } });
        tbody.appendChild(el('tr', {}, [
          el('td', { text: row.username || '（未知）' }),
          el('td', { class: 'mono', text: shortId(row.member_openid) }),
          el('td', { text: fmtTime(row.until_ts) }),
          el('td', { text: row.source || '-' }),
          el('td', { text: row.reason || '-' }),
          el('td', {}, [unmute]),
        ]));
      });
      mutesBox.appendChild(el('div', { class: 'table-wrap' }, [
        el('table', {}, [
          el('thead', {}, [el('tr', {}, ['成员', 'OpenID', '到期', '来源', '理由', '操作'].map((text) => el('th', { text })))]),
          tbody,
        ]),
      ]));
      const syncBtn = el('button', { class: 'btn ghost small', text: '与平台对账', onclick: async () => {
        try {
          const result = await bridge.apiPost('mutes/sync', { group_id: groupSelect.value });
          toast(result.ok ? '已对账，平台禁言 ' + (result.count || 0) + ' 人' : '对账失败：' + result.message, result.ok ? 'ok' : 'bad');
          await loadMutes();
        } catch (error) { toast('对账失败：' + error.message, 'bad'); }
      } });
      mutesBox.appendChild(el('div', { class: 'field-actions' }, [syncBtn]));
    } catch (error) {
      mutesBox.appendChild(notice('加载失败：' + error.message, 'bad'));
    }
  };

  const loadBlacklist = async () => {
    clear(blacklistBox);
    try {
      const data = await bridge.apiGet('blacklist', { group_id: groupSelect.value });
      blacklistBox.appendChild(el('div', { class: 'grid cols-2' }, [
        el('div', { class: 'notice' }, [
          el('div', { text: '平台黑名单（' + (data.platform || []).length + '）' }),
          el('div', { class: 'mono', text: (data.platform || []).map((item) => (item.username || '') + ' ' + shortId(item.member_openid)).join('；') || '（空）' }),
          data.error ? el('div', { class: 'muted', text: '接口不可用：' + data.error }) : null,
        ]),
        el('div', { class: 'notice' }, [
          el('div', { text: '本地黑名单（' + (data.local || []).length + '）' }),
          el('div', { class: 'mono', text: (data.local || []).map((openid) => shortId(openid)).join('；') || '（空）' }),
          el('div', { class: 'muted', text: '本地黑名单只影响插件判定（自动拒绝入群、命中即处置）。' }),
        ]),
      ]));
      const removeBtn = el('button', { class: 'btn small danger', text: '移除成员（内邀能力）', onclick: async () => {
        if (!(await uiConfirm('将调用平台的批量移除接口，操作不可撤销。确认继续？'))) return;
        const openid = await uiPrompt('请输入要移除的 member_openid：');
        if (!openid) return;
        try {
          const result = await bridge.apiPost('members/remove', {
            group_id: groupSelect.value,
            member_openids: [openid.trim()],
            add_to_blacklist: false,
          });
          toast('移除成功：' + JSON.stringify(result.response || {}), 'ok');
        } catch (error) { toast('移除失败：' + error.message, 'bad'); }
      } });
      blacklistBox.appendChild(el('div', { class: 'field-actions' }, [removeBtn]));
    } catch (error) {
      blacklistBox.appendChild(notice('加载失败：' + error.message, 'bad'));
    }
  };

  const loadGlobalBlacklist = async () => {
    clear(globalBlacklistBox);
    try {
      const data = await bridge.apiGet('global_blacklist');
      const entries = data.entries || [];
      const tbody = el('tbody');
      entries.forEach((row) => {
        const remove = el('button', { class: 'btn small danger', text: '解除', onclick: async () => {
          if (!(await uiConfirm('解除后该成员可再次申请入群，确认？'))) return;
          try {
            await bridge.apiPost('global_blacklist/update', { op: 'remove', openid: row.openid });
            toast('已解除', 'ok');
            await loadGlobalBlacklist();
          } catch (error) { toast('解除失败：' + error.message, 'bad'); }
        } });
        tbody.appendChild(el('tr', {}, [
          el('td', { text: row.masked || shortId(row.openid) }),
          el('td', { text: row.reason || '-' }),
          el('td', { text: row.added_by || '-' }),
          el('td', { text: fmtTime(row.added_at) }),
          el('td', {}, [remove]),
        ]));
      });
      const addBtn = el('button', { class: 'btn small', text: '添加', onclick: async () => {
        const openid = await uiPrompt('请输入要加入跨群黑名单的 member_openid：');
        if (!openid) return;
        const reason = await uiPrompt('理由（可留空）：') || '';
        try {
          await bridge.apiPost('global_blacklist/update', { op: 'add', openid: openid.trim(), reason });
          toast('已加入跨群黑名单', 'ok');
          await loadGlobalBlacklist();
        } catch (error) { toast('添加失败：' + error.message, 'bad'); }
      } });
      globalBlacklistBox.appendChild(el('div', { class: 'muted', text: '跨群黑名单对全插件生效：命中后任意群的入群申请都会被自动拒绝（不会自动移出已在群成员）。' }));
      globalBlacklistBox.appendChild(el('div', { class: 'table-wrap' }, [
        el('table', {}, [
          el('thead', {}, [el('tr', {}, ['成员', '理由', '操作人', '加入时间', '操作'].map((text) => el('th', { text })))]),
          tbody,
        ]),
      ]));
      globalBlacklistBox.appendChild(el('div', { class: 'field-actions' }, [addBtn]));
    } catch (error) {
      globalBlacklistBox.appendChild(notice('加载失败：' + error.message, 'bad'));
    }
  };

  const searchBtn = el('button', { class: 'btn ghost', text: '查询成员', onclick: async () => {
    searchBtn.disabled = true;
    try {
      const data = await bridge.apiGet('members/search', {
        group_id: groupSelect.value,
        q: searchInput.value.trim(),
      });
      searchOut.textContent = JSON.stringify(data, null, 2);
    } catch (error) { searchOut.textContent = '查询失败：' + error.message; }
    finally { searchBtn.disabled = false; }
  } });

  root.appendChild(card('禁言台账', null, [mutesBox]));
  root.appendChild(card('黑名单', null, [blacklistBox]));
  root.appendChild(card('跨群黑名单', '对全插件生效：命中后任意群的入群申请都会被自动拒绝；不会自动移出已在群成员。', [globalBlacklistBox]));
  root.appendChild(card('成员查询', '优先查本地缓存（群消息里见过的成员）；输入完整 openid 时会调用平台成员接口（内邀能力）。', [
    el('div', { class: 'row' }, [searchInput, el('div', { class: 'field-actions' }, [searchBtn])]),
    searchOut,
  ]));

  await Promise.all([loadMutes(), loadBlacklist(), loadGlobalBlacklist()]);
}

/* ------------------------------------------------------- 入群审批视图 */

async function viewJoins(root) {
  const config = await loadConfig();
  const settings = config.settings || {};
  const groups = config.groups || [];
  clear(root);
  if (!groups.length) {
    root.appendChild(notice('还没有登记任何群：让机器人在群里收到一条消息后再回到这里。'));
    return;
  }
  const selected = (config.ui_state || {}).joins_group || groups[0].group_id;
  const groupSelect = el('select');
  groups.forEach((group) => {
    groupSelect.appendChild(el('option', {
      value: group.group_id,
      text: (group.name || shortId(group.group_id)),
      selected: group.group_id === selected ? 'selected' : null,
    }));
  });
  groupSelect.addEventListener('change', async () => {
    await bridge.apiPost('ui_state', { joins_group: groupSelect.value });
    state.config = null;
    await render();
  });

  const modeSelect = el('select');
  (config.options && config.options.join_modes ? config.options.join_modes : ['off']).forEach((mode) => {
    modeSelect.appendChild(el('option', {
      value: mode,
      text: mode,
      selected: mode === (groups.find((item) => item.group_id === selected) || {}).effective_join_mode ? 'selected' : null,
    }));
  });
  const modeSave = el('button', { class: 'btn small', text: '应用模式', onclick: async () => {
    try {
      const snapshot = config.groups || [];
      const next = snapshot.map((group) => (group.group_id === groupSelect.value
        ? Object.assign({}, group, { join_review_mode: modeSelect.value })
        : group));
      await bridge.apiPost('groups/join_mode', { group_id: groupSelect.value, mode: modeSelect.value });
      state.config = null;
      toast('入群审批模式已更新', 'ok');
      await render();
    } catch (error) { toast('更新失败：' + error.message, 'bad'); }
  } });

  const fetchBtn = el('button', { class: 'btn ghost', text: '立即拉取申请', onclick: async () => {
    fetchBtn.disabled = true;
    try {
      const result = await bridge.apiPost('joins/fetch', { group_id: groupSelect.value });
      toast(result.ok ? '已拉取，新增待审 ' + ((result.created || []).length) : '拉取失败：' + result.message, result.ok ? 'ok' : 'bad');
      await render();
    } catch (error) { toast('拉取失败：' + error.message, 'bad'); }
    finally { fetchBtn.disabled = false; }
  } });

  root.appendChild(card('入群审批', '插件通过轮询获取入群申请（平台不推送该事件）；官方策略命中的申请不会出现在这里。', [
    el('div', { class: 'row' }, [
      groupSelect, modeSelect,
      el('div', { class: 'field-actions' }, [modeSave, fetchBtn]),
    ]),
  ]));

  /* 申请人画像与门槛配置 */
  const selectField = (label, pairs, value) => {
    const select = el('select');
    pairs.forEach((pair) => select.appendChild(el('option', {
      value: pair[0],
      text: pair[1],
      selected: pair[0] === value ? 'selected' : null,
    })));
    return { node: el('label', { class: 'field' }, [el('span', { text: label }), select]), input: select };
  };
  const profileEnabled = checkField('启用申请人画像采集（OneBot 可取 QQ 等级/账号年龄/头像）', settings.join_profile_enabled !== false);
  const requireQid = checkField('要求有 QID', settings.join_require_qid);
  const declineBlacklist = checkField('自动拒绝时加入群黑名单（内邀能力，可能失败）', settings.join_decline_blacklist !== false);
  const trustInviter = checkField('信任邀请人：被邀请入群直接通过', settings.join_trust_inviter);
  const minDays = numField('账号年龄门槛（天，0=关闭）', settings.join_min_account_days || 0, 0, 3650, 1);
  const minLevel = numField('QQ 等级门槛（0=关闭）', settings.join_min_qq_level || 0, 0, 144, 1);
  const gateAction = selectField('门槛命中动作', [['decline', '自动拒绝'], ['manual', '转人工'], ['pass', '放行']], settings.join_gate_action || 'decline');
  const missingPolicy = selectField('资料缺失策略（仅对可提供画像的通道生效）', [['manual', '转人工'], ['pass', '放行'], ['decline', '拒绝']], settings.join_profile_missing || 'manual');
  const avatarReview = selectField('头像多模态复核', [['off', '关闭'], ['approve_only', '仅复核拟放行的'], ['always', '每次都复核']], settings.join_avatar_review || 'off');
  const avatarBelow = numField('头像复核触发阈值（置信度低于此值才复核）', settings.join_avatar_only_below === undefined ? 0.95 : settings.join_avatar_only_below, 0, 1, 0.05);
  const minConfidence = numField('自动审批置信度门槛', settings.join_min_confidence === undefined ? 0.8 : settings.join_min_confidence, 0, 1, 0.05);
  const pollInterval = numField('轮询间隔（秒，仅官方通道）', settings.join_poll_interval || 60, 30, 600, 5);
  const cacheDays = numField('画像缓存（天）', settings.join_profile_cache_days || 7, 0, 90, 1);
  const profileQpm = numField('画像调用限频（次/分钟）', settings.join_profile_qpm || 30, 1, 300, 1);
  const profileConcurrency = numField('画像调用并发', settings.join_profile_concurrency || 2, 1, 8, 1);
  /* 入群答案校验：三项规则全空 = 不校验，保持旧版行为 */
  const textField = (label, value, placeholder) => {
    const input = el('input', { type: 'text', value: value || '', placeholder: placeholder || '' });
    return { node: el('label', { class: 'field' }, [el('span', { text: label }), input]), input };
  };
  const areaField = (label, text, placeholder) => {
    const input = el('textarea', { rows: '3', class: 'mono', placeholder: placeholder || '' });
    input.value = text || '';
    return { node: el('label', { class: 'field' }, [el('span', { text: label }), input]), input };
  };
  const expectedAnswer = textField('期望答案（留空=不校验，子串匹配）', settings.join_expected_answer, '例：ACM');
  const answerKeywords = areaField('答案关键词（一行一个，命中任一即可）', (settings.join_answer_keywords || []).join('\n'), '例：ACM\n校赛');
  const answerRegex = textField('答案正则（留空=不校验）', settings.join_answer_regex, '例：^AC[0-9]{4}$');
  const answerAction = selectField('答案校验未通过时', [['manual', '转人工（推荐）'], ['decline', '自动拒绝'], ['pass', '放行']], settings.join_answer_action || 'manual');
  const answerCase = checkField('答案校验区分大小写', settings.join_answer_case_sensitive);
  const saveJoinSettings = el('button', { class: 'btn', text: '保存入群审批配置', onclick: async () => {
    saveJoinSettings.disabled = true;
    try {
      await bridge.apiPost('joins/settings', {
        join_profile_enabled: profileEnabled.input.checked,
        join_require_qid: requireQid.input.checked,
        join_decline_blacklist: declineBlacklist.input.checked,
        join_trust_inviter: trustInviter.input.checked,
        join_min_account_days: Number(minDays.input.value),
        join_min_qq_level: Number(minLevel.input.value),
        join_gate_action: gateAction.input.value,
        join_profile_missing: missingPolicy.input.value,
        join_avatar_review: avatarReview.input.value,
        join_avatar_only_below: Number(avatarBelow.input.value),
        join_min_confidence: Number(minConfidence.input.value),
        join_poll_interval: Number(pollInterval.input.value),
        join_profile_cache_days: Number(cacheDays.input.value),
        join_profile_qpm: Number(profileQpm.input.value),
        join_profile_concurrency: Number(profileConcurrency.input.value),
        join_expected_answer: expectedAnswer.input.value.trim(),
        join_answer_keywords: answerKeywords.input.value.split('\n').map((line) => line.trim()).filter(Boolean),
        join_answer_regex: answerRegex.input.value.trim(),
        join_answer_action: answerAction.input.value,
        join_answer_case_sensitive: answerCase.input.checked,
      });
      state.config = null;
      toast('入群审批配置已保存', 'ok');
      await render();
    } catch (error) { toast('保存失败：' + error.message, 'bad'); }
    finally { saveJoinSettings.disabled = false; }
  } });
  root.appendChild(card('入群审批配置',
    '画像与门槛默认全关；官方通道不提供头像/账号等级，「资料缺失策略」对其不生效。入群答案校验三项留空即关闭。',
    [
      el('div', { class: 'row' }, [profileEnabled.node, requireQid.node, declineBlacklist.node, trustInviter.node]),
      el('div', { class: 'row' }, [minDays.node, minLevel.node, gateAction.node, missingPolicy.node]),
      el('div', { class: 'row' }, [avatarReview.node, avatarBelow.node, minConfidence.node, pollInterval.node]),
      el('div', { class: 'row' }, [cacheDays.node, profileQpm.node, profileConcurrency.node]),
      el('div', { class: 'row' }, [expectedAnswer.node, answerKeywords.node]),
      el('div', { class: 'row' }, [answerRegex.node, answerAction.node, answerCase.node]),
      el('div', { class: 'field-actions' }, [saveJoinSettings]),
    ]));

  let snapshot = null;
  try {
    snapshot = await bridge.apiGet('joins', { group_id: groupSelect.value });
  } catch (error) {
    root.appendChild(notice('加载入群申请失败：' + error.message, 'bad'));
    return;
  }

  const conflicts = (snapshot.conflicts || {}).conflicts || [];
  if (conflicts.length) {
    root.appendChild(notice('官方入群自动审批策略与插件自动审批同时生效，可能重复处理：' + conflicts.map(shortId).join('、'), 'warn'));
  }
  if ((snapshot.conflicts || {}).checked === false) {
    root.appendChild(notice('无法读取官方策略列表：' + ((snapshot.conflicts || {}).error || '未知原因') + '（不影响插件自身审批）'));
  }

  const pending = snapshot.pending || [];
  const decision = async (item, op) => {
    const request = item.request || {};
    const reason = op === 'decline' ? ((await uiPrompt('拒绝理由（可选，会展示给申请人）：')) || '') : '';
    try {
      await bridge.apiPost('joins/decide', {
        group_id: item.group_id,
        member_openid: request.member_openid,
        join_request_id: request.join_request_id,
        op,
        reason,
        blacklist: op === 'decline' ? await uiConfirm('同时加入群黑名单？（内邀能力，可能失败）') : false,
      });
      toast(op === 'approve' ? '已通过' : '已拒绝', 'ok');
      await render();
    } catch (error) { toast('审批失败：' + error.message, 'bad'); }
  };

  const progress = snapshot.status || {};
  const pendingBody = el('tbody');
  pending.forEach((item) => {
    const request = item.request || {};
    const verify = request.verify_info || {};
    const profile = request.profile || {};
    pendingBody.appendChild(el('tr', {}, [
      el('td', {}, [el('div', { class: 'row' }, [
        profile.avatar_url
          ? el('img', {
            src: profile.avatar_url,
            style: 'width:28px;height:28px;border-radius:50%;object-fit:cover;',
            onerror: (event) => { event.target.style.visibility = 'hidden'; },
          })
          : el('span', { class: 'muted', text: '—' }),
        el('span', { text: request.username || '未知' }),
      ])]),
      el('td', { text: fmtProfileNumber(profile, 'qq_level') }),
      el('td', { text: fmtProfileAge(profile) }),
      el('td', { text: request.apply_source === 'invited' ? '被邀请' : '主动申请' }),
      el('td', {}, [
        el('div', { text: (verify.verify_message || '（无）').slice(0, 60) }),
        el('div', {
          class: 'muted mono',
          text: (verify.review_qa_list || []).map((qa) => '问：' + ((qa || {}).question || '（无）') + ' → 答：' + ((qa || {}).answer || '（无）')).join(' ; '),
        }),
      ]),
      el('td', { text: request.risk_tips || '无' }),
      el('td', { text: ((item.decision || {}).reason || '-').slice(0, 30) }),
      el('td', {}, [el('div', { class: 'field-actions' }, [
        el('button', { class: 'btn small', text: '通过', onclick: () => decision(item, 'approve') }),
        el('button', { class: 'btn small danger', text: '拒绝', onclick: () => decision(item, 'decline') }),
      ])]),
    ]));
  });
  root.appendChild(card('待人工审批（' + pending.length + '）', '轮询 ' + (progress.polls || 0) + ' 次，累计获取 ' + (progress.fetched || 0) + ' 条申请', pending.length ? [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['申请人', 'QQ等级', '账号年龄', '来源', '验证消息 / 问答', '风险提示', '机器建议', '操作'].map((text) => el('th', { text })))]),
        pendingBody,
      ]),
    ]),
  ] : [notice('当前没有待人工审批的申请。')]));

  const history = snapshot.history || [];
  const historyBody = el('tbody');
  history.slice(0, 30).forEach((row) => {
    historyBody.appendChild(el('tr', {}, [
      el('td', { text: fmtTime(row.ts_unix ? new Date(row.ts_unix * 1000).toISOString() : '') }),
      el('td', { text: row.username || '未知' }),
      el('td', { text: fmtProfileNumber(row.profile || {}, 'qq_level') }),
      el('td', { text: fmtProfileAge(row.profile || {}) }),
      el('td', { text: row.decision || '-' }),
      el('td', { text: row.decided_by || '-' }),
      el('td', { text: typeof row.confidence === 'number' ? row.confidence.toFixed(2) : '-' }),
      el('td', { text: (row.reason || '').slice(0, 40) }),
    ]));
  });
  root.appendChild(card('历史记录（' + history.length + '）', null, history.length ? [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['时间', '申请人', 'QQ等级', '账号年龄', '决策', '决策方', '置信度', '原因'].map((text) => el('th', { text })))]),
        historyBody,
      ]),
    ]),
  ] : [notice('暂无记录。')]));

  let policyData = null;
  try {
    policyData = await bridge.apiGet('policy');
  } catch (error) { policyData = { strategies: [], error: error.message }; }
  const policyBody = el('tbody');
  (policyData.strategies || []).forEach((item) => {
    const toggle = el('button', {
      class: 'btn small ghost',
      text: String(item.is_enable).toLowerCase() === 'on' ? '停用' : '启用',
      onclick: async () => {
        try {
          await bridge.apiPost('policy', {
            op: String(item.is_enable).toLowerCase() === 'on' ? 'disable' : 'enable',
            strategy_id: item.strategy_id,
          });
          toast('已更新策略状态', 'ok');
          await render();
        } catch (error) { toast('更新失败：' + error.message, 'bad'); }
      },
    });
    const exec = el('button', { class: 'btn small ghost', text: '全量扫描', onclick: async () => {
      try {
        await bridge.apiPost('policy', { op: 'execute', strategy_id: item.strategy_id });
        toast('已触发全量扫描（官方说明约 10 分钟完成）', 'ok');
      } catch (error) { toast('触发失败：' + error.message, 'bad'); }
    } });
    const whitelist = el('button', { class: 'btn small ghost', text: '白名单', onclick: async () => {
      const raw = await uiPrompt('输入要新增的白名单 QQ 号（逗号分隔，留空则改为删除模式）：');
      if (raw === null) return;
      try {
        if (raw.trim()) {
          await bridge.apiPost('policy', {
            op: 'whitelist_add',
            strategy_id: item.strategy_id,
            users: raw.split(',').map((item2) => item2.trim()).filter(Boolean),
          });
          toast('已新增白名单号码', 'ok');
        } else {
          const del = (await uiPrompt('输入要删除的白名单 QQ 号（逗号分隔）：')) || '';
          await bridge.apiPost('policy', {
            op: 'whitelist_del',
            strategy_id: item.strategy_id,
            users: del.split(',').map((item2) => item2.trim()).filter(Boolean),
          });
          toast('已删除白名单号码', 'ok');
        }
        await render();
      } catch (error) { toast('白名单更新失败：' + error.message, 'bad'); }
    } });
    policyBody.appendChild(el('tr', {}, [
      el('td', { class: 'mono', text: item.strategy_id }),
      el('td', { text: String(item.is_enable).toLowerCase() === 'on' ? '启用中' : '已停用' }),
      el('td', { text: ((item.group_openids || []).map(shortId).join('、')) || ((item.group_ids || []).join('、')) || '-' }),
      el('td', { text: String(item.whitelist_user_count || 0) }),
      el('td', { text: fmtTime(item.expire_at) }),
      el('td', {}, [el('div', { class: 'field-actions' }, [toggle, exec, whitelist])]),
    ]));
  });
  root.appendChild(card('官方入群自动审批策略', policyData.error ? '读取失败：' + policyData.error : '插件默认不创建策略，只做透明化展示与白名单维护。', (policyData.strategies || []).length ? [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['策略 ID', '状态', '关联群', '白名单数', '到期', '操作'].map((text) => el('th', { text })))]),
        policyBody,
      ]),
    ]),
  ] : [notice(policyData.error ? '无法读取策略列表（接口可能未开放）。' : '当前没有任何策略。')]));
}

/* --------------------------------------------------------- 申诉处理视图 */

async function viewAppeals(root) {
  const config = await loadConfig();
  const groups = config.groups || [];
  clear(root);

  const stateSelect = el('select');
  [['pending', '待处理'], ['accepted', '已通过'], ['rejected', '已驳回'], ['all', '全部']]
    .forEach((pair) => {
      stateSelect.appendChild(el('option', { value: pair[0], text: pair[1] }));
    });
  const groupSelect = el('select');
  groupSelect.appendChild(el('option', { value: '', text: '全部群' }));
  groups.forEach((group) => {
    groupSelect.appendChild(el('option', {
      value: group.group_id,
      text: group.name || shortId(group.group_id),
    }));
  });
  const daysSelect = el('select');
  [7, 30, 90].forEach((days) => {
    daysSelect.appendChild(el('option', { value: String(days), text: '近 ' + days + ' 天', selected: days === 30 ? 'selected' : null }));
  });

  const box = el('div', { class: 'loading', text: '正在加载申诉…' });
  const load = async () => {
    clear(box);
    box.appendChild(el('div', { class: 'loading', text: '正在加载申诉…' }));
    try {
      const data = await bridge.apiGet('appeals', {
        state: stateSelect.value,
        group_id: groupSelect.value || undefined,
        days: daysSelect.value,
        limit: 200,
      });
      clear(box);
      const items = data.items || [];
      if (!items.length) {
        box.appendChild(notice('该筛选条件下没有申诉记录。'));
        return;
      }
      const decide = async (row, op) => {
        const note = (await uiPrompt(op === 'accept' ? '通过备注（可选）：' : '驳回理由（可选，会回执申诉人）：')) || '';
        try {
          await bridge.apiPost('appeals/decide', { event_id: row.id, op, note });
          toast(op === 'accept' ? '已通过（若原事件产生过禁言会自动解禁）' : '已驳回', 'ok');
          await load();
        } catch (error) { toast('处理失败：' + error.message, 'bad'); }
      };
      const tbody = el('tbody');
      items.forEach((row) => {
        const detail = el('button', { class: 'btn small ghost', text: '查看原文', onclick: async () => {
          await uiNotice('原消息', row.text_excerpt || '（未保存正文；可开启 store_text 保存完整正文）');
        } });
        const acceptBtn = el('button', { class: 'btn small', text: '通过', onclick: () => decide(row, 'accept') });
        const rejectBtn = el('button', { class: 'btn small danger', text: '驳回', onclick: () => decide(row, 'reject') });
        const stateLabel = { pending: '待处理', accepted: '已通过', rejected: '已驳回' }[row.appeal_state] || (row.appeal_state || '-');
        tbody.appendChild(el('tr', {}, [
          el('td', { text: fmtTime(row.ts || (row.ts_unix ? new Date(row.ts_unix * 1000).toISOString() : '')) }),
          el('td', { text: row.group_name || shortId(row.group_id) }),
          el('td', { text: row.sender_name || shortId(row.sender_openid) }),
          el('td', { text: (row.appeal_text || '').slice(0, 40) }),
          el('td', { text: (row.category || '-') + ' / ' + (row.verdict || '-') }),
          el('td', { text: stateLabel + (row.appeal_by ? '（' + row.appeal_by + '）' : '') }),
          el('td', {}, [el('div', { class: 'field-actions' }, [detail, acceptBtn, rejectBtn])]),
        ]));
      });
      box.appendChild(el('div', { class: 'table-wrap' }, [el('table', {}, [
        el('thead', {}, [el('tr', {}, ['时间', '群', '申诉人', '理由', '原判', '状态', '操作'].map((text) => el('th', { text })))]),
        tbody,
      ])]));
    } catch (error) {
      clear(box);
      box.appendChild(notice('加载失败：' + error.message, 'bad'));
    }
  };

  root.appendChild(card('申诉处理',
    '成员回复被处置消息发送「申诉 <理由>」；这里的「通过」会自动解禁（若原事件产生过禁言）并追加 appeal_accepted 留痕，不删除原审核事件。', [
    el('div', { class: 'row' }, [
      stateSelect, groupSelect, daysSelect,
      el('div', { class: 'field-actions' }, [el('button', { class: 'btn ghost', text: '刷新', onclick: () => load() })]),
    ]),
    box,
  ]));
  await load();
}

/* ------------------------------------------------------------- 占位视图 */

function viewComingSoon(root, view) {
  clear(root);
  root.appendChild(card(view.label, null, [
    notice('该视图将在 ' + view.soon + ' 版本提供：' + view.label + '。当前版本（M1）已交付能力探测、群列表、日志中心与工具页。'),
  ]));
}

/* ------------------------------------------------------------------ 路由 */

async function render() {
  const viewId = location.hash.replace('#/', '') || 'dashboard';
  const view = VIEWS.find((item) => item.id === viewId) || VIEWS[0];
  const root = document.getElementById('content');
  renderNav();
  try {
    await loadConfig(true);
    renderTopbar();
  } catch (error) {
    clear(root);
    root.appendChild(notice('无法读取插件配置：' + error.message + '（请确认插件已启用并在 WebUI 中重载过）', 'bad'));
    return;
  }
  if (view.id === 'dashboard') await viewDashboard(root);
  else if (view.id === 'groups') await viewGroups(root);
  else if (view.id === 'logs') await viewLogs(root);
  else if (view.id === 'tools') await viewTools(root);
  else if (view.id === 'policy') await viewPolicy(root);
  else if (view.id === 'keywords') await viewKeywords(root);
  else if (view.id === 'rulesx') await viewRulesX(root);
  else if (view.id === 'members') await viewMembers(root);
  else if (view.id === 'joins') await viewJoins(root);
  else if (view.id === 'appeals') await viewAppeals(root);
  else viewComingSoon(root, view);
}

async function boot() {
  try {
    if (bridge && typeof bridge.ready === 'function') await bridge.ready();
  } catch (error) {
    // 忽略：bridge 不可用时仍尝试直接请求
  }
  document.getElementById('btn-refresh').addEventListener('click', async () => {
    state.config = null;
    state.logs.data = null;
    await render();
    toast('已刷新', 'ok');
  });
  window.addEventListener('hashchange', render);
  await render();
}

boot();
