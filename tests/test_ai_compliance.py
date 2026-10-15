"""QQ群管合规用例：AI 事件不计入审核量、配置契约（默认值+页面+边界）、错误形态。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import ai_common
from src.audit import AuditStore
from src.models import NUMERIC_BOUNDS, default_settings

ROOT = Path(__file__).resolve().parent.parent
AI_KEYS = (
    "qqgm_ai_tools_enabled",
    "qqgm_ai_tools_read_enabled",
    "qqgm_ai_tools_write_enabled",
    "qqgm_ai_throttle_limit",
    "qqgm_ai_throttle_window",
    "qqgm_ai_respect_lenient",
)


def run(coro):
    return asyncio.run(coro)


async def make_store(tmp_path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.db", flush_interval=0.05)
    await store.initialize()
    return store


def test_summary_excludes_ai_tool_events(tmp_path):
    """AI 工具产生的动作不得抬高「审核量」。"""

    async def scenario():
        store = await make_store(tmp_path)
        await store.insert_event(
            group_id="g1", group_name="G1", sender_openid="u1", sender_name="A",
            source="llm", verdict="violation", category="广告", severity=4, reason="规则命中",
        )
        await store.insert_event(
            group_id="g1", group_name="G1", sender_openid="u2", sender_name="B",
            source="llm_tool", verdict="manual", category="人工", severity=3, reason="AI 禁言",
        )
        await store.flush()
        summary = await store.summary(1)
        assert summary["events_total"] == 1, summary["verdicts"]
        assert "manual" not in summary["verdicts"]
        by_category = await store.summary_by_category(1)
        assert sum(int(row["total"]) for row in by_category) == 1
        by_group = await store.summary_by_group(1)
        assert sum(int(row["total"]) for row in by_group) == 1
        await store.close()

    run(scenario())


def test_find_last_event_ignores_ai_tool_events(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        await store.insert_event(
            group_id="g1", sender_openid="u1", sender_name="A",
            source="llm_tool", verdict="manual", category="人工", severity=3,
        )
        assert await store.find_last_event("g1", "u1") is None
        await store.close()

    run(scenario())


def test_config_contract_defaults_ui_and_bounds() -> None:
    settings = default_settings()
    app_js = (ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    for key in AI_KEYS:
        assert key in settings, "默认值缺 " + key
        assert key in app_js, "后台页面缺控件 " + key
    assert NUMERIC_BOUNDS["qqgm_ai_throttle_limit"] == (1, 60)
    assert NUMERIC_BOUNDS["qqgm_ai_throttle_window"] == (5, 3600)
    assert settings["qqgm_ai_tools_write_enabled"] is False, "写工具必须默认关"


def test_bool_keys_are_coerced() -> None:
    source = (ROOT / "src" / "store.py").read_text(encoding="utf-8")
    for key in ("qqgm_ai_tools_enabled", "qqgm_ai_tools_read_enabled", "qqgm_ai_tools_write_enabled", "qqgm_ai_respect_lenient"):
        assert key in source, "store 布尔归一化缺 " + key


def test_error_payload_shape() -> None:
    payload = json.loads(ai_common.fail("permission_denied"))
    assert payload["ok"] is False and payload["error"]["message"]
    assert ai_common.parse_int("99", 1, 30) is None


def test_astrbot_version_declared() -> None:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"astrbot_version:\s*\"?([^\"\n]+)", text)
    assert match and match.group(1).strip().startswith(">=4")


def test_readme_has_no_mandatory_install_claim() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for bad in ("必须同时安装", "必须先安装其它插件"):
        assert bad not in readme


def test_version_consistent_across_metadata_main_and_changelog() -> None:
    """版本三处一致：metadata.yaml / main.py 的 VERSION / CHANGELOG 顶部。"""
    root = Path(__file__).resolve().parent.parent
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    from_meta = ""
    for line in metadata.splitlines():
        if line.startswith("version:"):
            from_meta = line.split(":", 1)[1].strip()
            break
    assert from_meta, "metadata.yaml 缺 version"
    source = (root / "main.py").read_text(encoding="utf-8")
    declared = ""
    for line in source.splitlines():
        if line.startswith("VERSION = "):
            declared = line.split("=", 1)[1].strip().strip(chr(34))
            break
    assert declared, "main.py 缺 VERSION 常量"
    assert declared == from_meta, (declared, from_meta)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert ("## [" + from_meta + "]") in changelog, "CHANGELOG 缺当前版本段"
