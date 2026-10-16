# 面板离线预览 / 渲染级冒烟

这套文件只做一件事：**让面板在没有 AstrBot 的情况下真跑起来**，从而抓住静态断言抓不到的缺陷
（渲染时抛异常、整页空白、术语泄漏）。

## 用法

```bash
python3 tools/panel_smoke.py            # 三个视图（总览/策略/入群审批）渲染冒烟，退出码 0/1
python3 -m http.server 8899             # 手动预览（需要先生成 config.json）
```

`tools/panel_smoke.py` 会：
1. 用插件自己的 `default_settings()` 生成 `config.json`（因此**永远跟上后端键**，不会漂）；
2. 复制 `pages/manage/*` 与这里的桩，起一个临时 http server；
3. 用 `chrome --headless=new --dump-dom` 打开 `#/dashboard`、`#/policy`、`#/joins`；
4. 断言：关键内容出现 ✓、内部术语不出现 ✗、`#__errhook` 里没有运行时错误 ✗、DOM 体积不低于下限 ✗。

没装 chrome 时自动 SKIP（退出码 0），`tests/test_panel_smoke.py` 同理。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | 预览骨架：注入 errhook + bridge 桩，加载 `app.js` |
| `bridge_stub.js` | bridge 桩：`config` 与 `joins` 快照（含一个「当前不可用」的备用模型） |
| `errhook.js` | 收集 JS 错误并**写进 DOM**（`--dump-dom` 拿不到 console，只能这样让错误可见） |

## 教训（为什么要固化它）

面板此前所有测试都是静态字符串断言；0.15.4 引入的 `provider_list` 字段没有原生 `input`，
入群审批页在 `built.input.disabled` 处抛错、整页只剩顶栏 —— 静态断言全绿，直到真浏览器跑了一次才发现。
每次改面板，**跑这个冒烟**，别只看 `node --check`（它只查语法，查不出未定义引用）。
