/**
 * 面板纯逻辑（不碰 DOM，可被 node 直接单测）。
 *
 * 为什么单独成文件：AstrBot 插件页服务会把相对 import 重写成带 token 的资源地址，
 * 所以多文件 ES module 是官方支持的；把可测逻辑放这里，node 能跑同一份代码，
 * 不需要浏览器、不需要打包工具。
 */

/** 勾选/取消一个备用模型；勾选时追加到末尾（末尾=优先级最低） */
export function toggleProvider(order, id, checked) {
  const list = Array.isArray(order) ? order.slice() : [];
  const key = String(id || '');
  if (!key) return list;
  const at = list.indexOf(key);
  if (checked) {
    if (at < 0) list.push(key);
  } else if (at >= 0) {
    list.splice(at, 1);
  }
  return list;
}

/** 上移/下移（delta = -1 / +1）；越界时原样返回，不抛错 */
export function moveProvider(order, id, delta) {
  const list = Array.isArray(order) ? order.slice() : [];
  const at = list.indexOf(String(id || ''));
  const to = at + Number(delta || 0);
  if (at < 0 || to < 0 || to >= list.length) return list;
  list.splice(at, 1);
  list.splice(to, 0, String(id));
  return list;
}

/** 候选列表：已配置但当前不存在的 id 也要显示出来（否则会被静默丢掉） */
export function mergeProviders(configured, items) {
  const list = (Array.isArray(items) ? items : []).filter(function (item) {
    return item && item.id;
  }).map(function (item) {
    // 保留入参已有的 missing 标记：复合用法（先 merge 再 decorate）曾经把标记丢掉，导致「当前不可用」永不显示
    return { id: String(item.id), model: String(item.model || ''), type: String(item.type || ''), missing: Boolean(item.missing) };
  });
  const known = {};
  list.forEach(function (item) { known[item.id] = true; });
  (Array.isArray(configured) ? configured : []).forEach(function (id) {
    const key = String(id || '');
    if (key && !known[key]) {
      known[key] = true;
      list.push({ id: key, model: key, type: '当前不可用', missing: true });
    }
  });
  return list;
}

/** 一个模型在界面上怎么显示：主标题优先用模型名，副标题才是那串长 id */
export function providerDisplay(item) {
  const it = item || {};
  const id = String(it.id || '');
  const model = String(it.model || '');
  const type = String(it.type || '');
  return {
    title: model || id || '（未命名模型）',
    subtitle: model && id && model !== id ? id : '',
    type: type,
  };
}

/** 已选顺序 -> 芯片数据（带 1 起的优先级序号） */
export function decorateProviders(order, items) {
  const pool = {};
  mergeProviders([], items).forEach(function (item) { pool[item.id] = item; });
  return (Array.isArray(order) ? order : []).map(function (id, index) {
    const info = providerDisplay(pool[id] || { id: id });
    return {
      id: String(id),
      title: info.title,
      subtitle: info.subtitle,
      type: info.type,
      priority: index + 1,
      missing: !pool[id] || Boolean(pool[id] && pool[id].missing),   // 入参 items 已被 mergeProviders 补过缺失项，不能只看 pool
    };
  });
}

/** 按钮上的字数：与 AstrBot 的「已选择 {count} 个提供商」同风格 */
export function selectionLabel(count) {
  const n = Number(count || 0);
  return n > 0 ? '已选择 ' + n + ' 个模型' : '未选择';
}

/** 搜索过滤：模型名、id、类型任意命中即可 */
export function matchProviders(items, keyword) {
  const text = String(keyword || '').trim().toLowerCase();
  const list = (Array.isArray(items) ? items : []).filter(function (item) { return item && item.id; });
  if (!text) return list.slice();
  return list.filter(function (item) {
    const hay = (
      String(item.model || '') + ' ' + String(item.id || '') + ' ' + String(item.type || '')
    ).toLowerCase();
    return hay.indexOf(text) >= 0;
  });
}


/** B3（P1-e）：风险分阈值只住在 send_conditions 的 risk>=N 里 —— 面板从条件串读，不再有影子键。 */
export function riskThresholdFromConditions(conditions, fallback) {
  const list = Array.isArray(conditions) ? conditions : [];
  for (let i = 0; i < list.length; i += 1) {
    const item = String(list[i] || '');
    if (item.indexOf('risk>=') === 0) {
      const value = Number(item.slice('risk>='.length));
      if (Number.isFinite(value)) return value;
    }
  }
  return typeof fallback === 'number' ? fallback : 60;
}

/** B3（P1-e）：启用时把 risk>=N 写进条件串，关闭时移除；其它条件原样保留、顺序不重排。 */
export function conditionsWithRisk(conditions, enabled, value) {
  const list = (Array.isArray(conditions) ? conditions : [])
    .map(function (item) { return String(item || ''); })
    .filter(function (item) { return item && item.indexOf('risk>=') !== 0; });
  if (!enabled) return list;
  const parsed = Number(value);
  const fallback = riskThresholdFromConditions(conditions, 60);
  // 清空输入框不得变成 risk>=1（那等于几乎全量送审）：非法/过小一律回退到原阈值或默认 60
  const n = Math.max(10, Math.round(Number.isFinite(parsed) && parsed > 0 ? parsed : fallback));
  return list.concat(['risk>=' + String(n)]);
}



/* ---------------- 通知会话候选（显式选择器用） ---------------- */

const SESSION_KIND_LABELS = { GroupMessage: "群聊", FriendMessage: "私聊", OtherMessage: "其他" };

/** 会话 ID 太长时截断展示（完整值仍用于填入）。 */
export function shortSessionId(umo, keep) {
  const parts = String(umo || "").split(":");
  if (parts.length < 3) return String(umo || "");
  const sid = parts.slice(2).join(":");
  const n = keep || 12;
  return sid.length > n ? sid.slice(0, n) + "…" : sid;
}

/** 候选展示名：平台实例 · 消息类型 短会话 ID。 */
export function sessionOptionLabel(item) {
  const umo = String((item && item.umo) || "");
  const parts = umo.split(":");
  if (parts.length >= 3) {
    const kind = SESSION_KIND_LABELS[parts[1]] || parts[1];
    return parts[0] + " · " + kind + " " + shortSessionId(umo, 12);
  }
  return String((item && item.label) || umo);
}

/** 关键词过滤：匹配原始 UMO、后端标签或展示名。 */
export function filterSessions(items, keyword) {
  const list = Array.isArray(items) ? items : [];
  const key = String(keyword || "").trim().toLowerCase();
  if (!key) return list.slice();
  return list.filter((item) => {
    const hay = (String(item && item.umo) + " " + String(item && item.label) + " " + sessionOptionLabel(item)).toLowerCase();
    return hay.indexOf(key) >= 0;
  });
}

/** 选择器底部提示：空列表 / 无匹配 / 计数。 */
export function sessionPickerHint(total, shown) {
  if (!total) return "暂无候选：先在群里或私聊发一条消息，然后点「刷新候选」。";
  if (!shown) return "没有匹配的会话（共 " + total + " 条候选）。";
  return "共 " + total + " 条候选，显示 " + shown + " 条。";
}
