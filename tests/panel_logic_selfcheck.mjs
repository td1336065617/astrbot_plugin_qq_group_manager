/**
 * 面板纯逻辑自检（node 直接跑：node tests/panel_logic_selfcheck.mjs）。
 *
 * 之所以放在 tests/ 而不是 pages/：这是测试夹具，不该出现在插件页面的资源里。
 * import 用相对路径，node 按文件位置解析，所以从任何 cwd 跑都一样。
 */

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

// 兜底处置：面向用户的一句话必须是人话，且不出现内部术语「地板」
const off = L.floorSentence({ review_floor_actions: [] });
if (off.indexOf("当前关闭") < 0 || off.indexOf("需要人工复核") < 0) {
  throw new Error("floor_off 文案不对：" + off);
}
if (off.indexOf("地板") >= 0) throw new Error("面向用户的文案里出现了内部术语「地板」");
const on = L.floorSentence({
  review_floor_actions: ["warn", "report"],
  review_floor_categories: ["辱骂攻击"],
  review_floor_cooldown_seconds: 600,
  review_floor_daily_limit: 20,
});
["当前开启", "警告 + 上报", "辱骂攻击", "600 秒", "20 次"].forEach((piece) => {
  if (on.indexOf(piece) < 0) throw new Error("floor_on 缺少『" + piece + "』：" + on);
});

// 品类差异化只列与总动作不同的
eq("overrides", L.floorOverrides({
  review_floor_actions: ["warn"],
  review_floor_by_category: { "色情低俗": ["warn", "report"], "辱骂攻击": ["warn"] },
}), ["色情低俗：警告 + 上报"]);

// 多选下拉的选项文案：第一项是跟随
eq("choices", L.FLOOR_ACTION_CHOICES.length, 4);
eq("choices_first", L.FLOOR_ACTION_CHOICES[0][0], "");
eq("categories", L.FLOOR_CATEGORIES.length, 4);

console.log("panel_logic 自检通过（" + "17 项断言）");
