/**
 * 面板纯逻辑（不碰 DOM，可被 node 直接单测）。
 *
 * 为什么单独成文件：AstrBot 插件页服务会把相对 import 重写成带 token 的资源地址，
 * 所以多文件 ES module 是官方支持的；把可测逻辑放这里，node 能跑同一份代码，
 * 不需要浏览器、不需要打包工具。
 */

export const FLOOR_CATEGORIES = [
  '色情低俗',
  '辱骂攻击',
  '政治敏感',
  '违法违规',
];

export const FLOOR_ACTION_LABELS = { warn: '警告', report: '上报' };

/** 品类差异化的下拉选项：第一项表示「跟随上面的执行动作」 */
export const FLOOR_ACTION_CHOICES = [
  ['', '跟随上面的执行动作'],
  ['warn', '只警告'],
  ['report', '只上报'],
  ['warn,report', '警告 + 上报'],
];

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
    return { id: String(item.id), model: String(item.model || ''), type: String(item.type || ''), missing: false };
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
      missing: !pool[id],
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

/** 兜底处置当前是什么状态，用一句大白话说出来（面板里实时显示） */
export function floorSentence(settings) {
  const s = settings || {};
  const actions = Array.isArray(s.review_floor_actions) ? s.review_floor_actions : [];
  const picked = actions.filter(function (a) { return FLOOR_ACTION_LABELS[a]; }).map(function (a) {
    return FLOOR_ACTION_LABELS[a];
  });
  if (!picked.length) {
    return '当前关闭：AI 只判出「需要人工复核」、又没命中任何硬规则时，插件只记一条日志，不在群里做任何动作。';
  }
  const cats = (Array.isArray(s.review_floor_categories) ? s.review_floor_categories : []).filter(Boolean);
  const scope = cats.length ? cats.join('、') : '所有品类';
  let text = '当前开启：' + scope + ' 里出现「需要人工复核」时，至少执行 ' + picked.join(' + ') + '。';
  const limits = [];
  const cooldown = Number(s.review_floor_cooldown_seconds || 0);
  const daily = Number(s.review_floor_daily_limit || 0);
  if (cooldown > 0) limits.push('同一成员 ' + cooldown + ' 秒内只兜底一次');
  if (daily > 0) limits.push('单群每天最多兜底 ' + daily + ' 次');
  if (limits.length) text += '防刷屏：' + limits.join('；') + '。';
  return text;
}

/** 品类差异化里「与总动作不同」的项，列出来给用户看 */
export function floorOverrides(settings) {
  const s = settings || {};
  const map = s.review_floor_by_category || {};
  const base = (Array.isArray(s.review_floor_actions) ? s.review_floor_actions : [])
    .filter(function (a) { return FLOOR_ACTION_LABELS[a]; })
    .map(function (a) { return FLOOR_ACTION_LABELS[a]; })
    .join(' + ');
  const out = [];
  FLOOR_CATEGORIES.forEach(function (cat) {
    const picked = map[cat];
    if (!Array.isArray(picked) || !picked.length) return;
    const text = picked.map(function (a) { return FLOOR_ACTION_LABELS[a] || a; }).join(' + ');
    if (text !== base) out.push(cat + '：' + text);
  });
  return out;
}
