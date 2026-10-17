/**
 * 面板纯逻辑自检（node 直接跑：node tests/panel_logic_selfcheck.mjs）。
 *
 * 之所以放在 tests/ 而不是 pages/：这是测试夹具，不该出现在插件页面的资源里。
 * import 用相对路径，node 按文件位置解析，所以从任何 cwd 跑都一样。
 */

import { readFileSync } from "node:fs";
const __src = readFileSync(new URL(import.meta.url), "utf8");
import * as L from "../pages/manage/panel_logic.js";

function eq(name, got, want) {
  const a = JSON.stringify(got);
  const b = JSON.stringify(want);
  if (a !== b) throw new Error(name + ": got " + a + ", want " + b);
}

// 勾选 / 取消 / 去重
eq("toggle", L.toggleProvider(["a"], "b", true), ["a", "b"]);
eq("toggle_off", L.toggleProvider(["a", "b"], "a", false), ["b"]);
eq("toggle_dup", L.toggleProvider(["a"], "a", true), ["a"]);

// 优先级调整：正常 / 越界不抛错
eq("move_up", L.moveProvider(["a", "b", "c"], "c", -1), ["a", "c", "b"]);
eq("move_edge", L.moveProvider(["a"], "a", -1), ["a"]);
eq("move_unknown", L.moveProvider(["a"], "zz", 1), ["a"]);

// 文案：与 AstrBot 的「已选择 {count} 个提供商」同风格
eq("label_2", L.selectionLabel(2), "已选择 2 个模型");
eq("label_0", L.selectionLabel(0), "未选择");

// 芯片：模型名做主标题，长 id 只做副标题，优先级从 1 开始
const chips = L.decorateProviders(["x"], [{ id: "x", model: "m1", type: "chat_completion" }]);
eq("chip_title", chips[0].title, "m1");
eq("chip_sub", chips[0].subtitle, "x");
eq("chip_prio", chips[0].priority, 1);

// 已配置但当前不存在的模型必须保留并标注，不能被静默丢掉
const merged = L.mergeProviders(["gone"], [{ id: "x", model: "m1" }]);
eq("merged_missing", merged.map((i) => i.missing), [false, true]);
eq("merged_ids", merged.map((i) => i.id), ["x", "gone"]);

// 搜索
eq("search", L.matchProviders([{ id: "a", model: "qq" }, { id: "b", model: "zz" }], "qq").map((i) => i.id), ["a"]);
eq("search_all", L.matchProviders([{ id: "a" }], "   ").length, 1);


const __n = (__src.match(/eq\(/g) || []).length;
console.log("panel_logic 自检通过（" + __n + " 项断言）");
// 复合用法（buildProviderList 的真实调用）：先 merge 再 decorate 也必须保留「当前不可用」
const cfg = ["gone/stale"];
const items = L.mergeProviders(cfg, [{ id: "x", model: "m1" }]);
const dec = L.decorateProviders(cfg, items);
eq("decorate_missing", dec.map((i) => i.missing), [true]);

// B3（P1-e）：阈值单一真源
eq("risk_from_conditions", L.riskThresholdFromConditions(["rule_hit", "risk>=70"], 60), 70);
eq("risk_from_conditions_default", L.riskThresholdFromConditions(["rule_hit"], 60), 60);
eq("risk_write", L.conditionsWithRisk(["rule_hit"], true, 85), ["rule_hit", "risk>=85"]);
eq("risk_off_removes", L.conditionsWithRisk(["rule_hit", "risk>=85"], false, 85), ["rule_hit"]);

/* ---------------- 通知会话候选（显式选择器） ---------------- */
const SESSIONS = [
  { umo: "爱莉希雅:GroupMessage:C8D6272C4A0007EE7AF4A8EF4E781AA9", label: "爱莉希雅 · 群聊 C8D6272C4A00" },
  { umo: "爱莉希雅2:FriendMessage:418181734", label: "爱莉希雅2 · 私聊 418181734" },
];

eq("label_kind", L.sessionOptionLabel(SESSIONS[0]), "爱莉希雅 · 群聊 C8D6272C4A00…");
eq("label_short", L.sessionOptionLabel(SESSIONS[1]), "爱莉希雅2 · 私聊 418181734");
eq("label_weird", L.sessionOptionLabel({ umo: "BROKEN" }), "BROKEN");
eq("shortid_keeps_colons", L.shortSessionId("a:FriendMessage:x:y", 12), "x:y");
eq("shortid_truncates", L.shortSessionId("a:FriendMessage:0123456789ABCDEF", 4), "0123…");

eq("filter_none", L.filterSessions(SESSIONS, "").length, 2);
eq("filter_platform", L.filterSessions(SESSIONS, "爱莉希雅2").length, 1);
eq("filter_by_umo", L.filterSessions(SESSIONS, "C8D6272C").length, 1);
eq("filter_by_kind", L.filterSessions(SESSIONS, "私聊").length, 1);
eq("filter_case", L.filterSessions(SESSIONS, "friendmessage").length, 1);
eq("filter_miss", L.filterSessions(SESSIONS, "zzz").length, 0);
eq("filter_bad_input", L.filterSessions(null, "x").length, 0);

eq("hint_empty", L.sessionPickerHint(0, 0).indexOf("暂无候选") >= 0, true);
eq("hint_miss", L.sessionPickerHint(31, 0).indexOf("没有匹配") >= 0, true);
eq("hint_count", L.sessionPickerHint(31, 12).indexOf("31") >= 0, true);
eq("risk_replace_keeps_order", L.conditionsWithRisk(["rule_hit", "risk>=10", "flood"], true, 20), ["rule_hit", "flood", "risk>=20"]);

// —— 处罚边界（0.15.24 起）：表单→配置、配置校验 ——
eq("boundary_form_null_and_num", L.boundaryPolicyFromForm({ 其他: { min: "", max: "2" } }), { 其他: { min: null, max: 2 } });
eq("boundary_form_zero_kept", L.boundaryPolicyFromForm({ 政治敏感: { min: "", max: "0" } }), { 政治敏感: { min: null, max: 0 } });
eq("boundary_form_empty", L.boundaryPolicyFromForm({}), {});
eq("boundary_ok", L.boundaryProblemFromPolicy({ 其他: { min: 1, max: 2 } }), "");
eq("boundary_bad_min_gt_max", L.boundaryProblemFromPolicy({ 其他: { min: 3, max: 1 } }).indexOf("不能大于") >= 0, true);
eq("boundary_null_max_skips_check", L.boundaryProblemFromPolicy({ 其他: { min: 3, max: null } }), "");
eq("boundary_max_zero_with_min", L.boundaryProblemFromPolicy({ 其他: { min: 1, max: 0 } }).indexOf("不能大于") >= 0, true);
