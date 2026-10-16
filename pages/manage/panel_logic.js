/**
 * 面板纯逻辑（不碰 DOM，可被 node 直接单测）。
 *
 * 为什么单独成文件：AstrBot 插件页服务会把相对 import 重写成带 token 的资源地址，
 * 所以多文件 ES module 是官方支持的；把可测逻辑放这里，node 能跑同一份代码，
 * 不需要浏览器、不需要打包工具。
 */

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
