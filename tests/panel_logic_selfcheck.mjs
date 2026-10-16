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


console.log("panel_logic 自检通过（" + "16 项断言）");
