# 隔离矩阵（R-IND-01 交付物）

```bash
PYTHON=/path/to/python bash tools/run_single_plugin.sh astrbot_plugin_qq_group_manager
```

| 用例 | 期望 | 结果 | 证据（时间/输出摘要） |
|---|---|---|---|
| 只装本插件启动 | 无异常；插件类可导入；工具已注册 | ✅ | 2026-10-15，`插件类：[QQGroupManager]`；真 AstrBot 逐条打印 `Added llm tool: qqgm_group_info / qqgm_moderation_status / qqgm_stats / qqgm_recent_events / qqgm_mute_user / qqgm_unmute_user` |
| 全量单测（隔离副本内） | 全绿 | ✅ | `521 passed, 1 skipped`（exit=0） |
| 零跨插件 import | 门禁 exit 0 | ✅ | `零耦合门禁通过（70 个文件）` |
| 不装其它三个插件 | 工具照常工作 | ✅ | 只读工具走本插件 store/audit；写工具走 `ActionExecutor.handle_manual` |
| 写工具默认关 | 未显式开启时拒绝执行且**零平台调用** | ✅ | `test_mute_requires_write_switch`（断言 `actions.calls == []`） |
| 权限隔离 | 非群管调用管理类工具被拒 | ✅ | `test_stats_requires_admin` / `test_recent_events_requires_admin_and_valid_limit` |
| 审计口径 | AI 事件不计入审核量、不可被申诉关联 | ✅ | `test_summary_excludes_ai_tool_events`（真实 sqlite：2 条事件 → 总量 1）/ `test_find_last_event_ignores_ai_tool_events` |
| dry-run 幂等 | 演练/失败不占用幂等键 | ✅ | `_seen(..., commit=False)` + 成功才 `_commit_seen`；`src/actions.py` |
