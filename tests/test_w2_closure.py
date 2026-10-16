"""W2 处置闭环：统计口径拆分、禁言台账 event_id、平台失败错误码。"""

from __future__ import annotations

import asyncio
import sqlite3

from src.api_client import SEM_UNKNOWN, QQApiError, QQGroupAPI, synthetic_err_code
from src.audit import AuditStore
from src.models import CAP_MUTE, CAP_RECALL, Verdict
from tests.test_actions import make_env


def run(coro):
    return asyncio.run(coro)


async def _store(tmp_path):
    store = AuditStore(tmp_path / "audit.db", flush_interval=0.05)
    await store.initialize()
    return store


def test_summary_splits_degraded_from_real_judgement(tmp_path):
    async def scenario():
        store = await _store(tmp_path)
        await store.insert_event(group_id="g1", verdict="allow", category="无", severity=1, confidence=0.9, stage="judged")
        await store.insert_event(group_id="g1", verdict="review", category="无", severity=1, confidence=0.9, stage="judged_kv")
        await store.insert_event(group_id="g1", verdict="review", category="无", severity=1, confidence=0.0, stage="api_error")
        await store.insert_event(group_id="g1", verdict="review", category="无", severity=1, confidence=0.0, stage="low_confidence")
        await store.insert_event(group_id="g1", verdict="violation", category="广告引流", severity=4, confidence=0.99, stage="rule")
        report = await store.summary(1)
        await store.close()
        return report

    report = run(scenario())
    assert report["stages"]["judged"] == 1
    assert report["stages"]["judged_kv"] == 1
    assert report["stages"]["rule"] == 1
    assert report["degraded_total"] == 2, "降级（没审成）只算 api_error/low_confidence 这类"


def test_upsert_mute_keeps_event_id(tmp_path):
    async def scenario():
        store = await _store(tmp_path)
        await store.upsert_mute(group_id="g1", member_openid="u1", until_unix=1, reason="测试", source="auto", active=True, event_id=4321)
        await store.close()
        conn = sqlite3.connect(tmp_path / "audit.db")
        row = conn.execute("select event_id, active from mutes").fetchone()
        conn.close()
        return row

    assert run(scenario()) == (4321, 1)


def test_mute_action_passes_event_id_to_ledger():
    """端到端：真实处置链路必须把事件 id 带进禁言台账（N4 修复的验收点）。"""
    store, _api, audit, actions, config = make_env(capabilities=(CAP_RECALL, CAP_MUTE), dry_run=False)
    run(store.update_settings({"mode": "standard"}))
    verdict = Verdict(verdict="violation", severity=4, category="广告引流", reason="含链接", confidence=0.95)
    result = run(actions.handle(group_id="g1", config=config, verdict=verdict, settings=store.settings(), msg_id="m2", sender_openid="u1", send=None))
    executed = {item["action"]: item for item in result["actions"]}
    assert executed["mute"]["ok"] is True
    row = audit.mutes.get(("g1", "u1"))
    assert row, "禁言必须写台账"
    assert row.get("event_id") == result["event_id"], "台账必须带上事件 id（N4 修复点）"


def test_synthetic_err_code_mapping():
    assert synthetic_err_code(QQApiError("x", err_code=1234)) == 1234, "平台错误码必须原样保留"
    assert synthetic_err_code(QQApiError("x", semantic=SEM_UNKNOWN)) == -9
    timeout_error = QQGroupAPI._error_from_exception(TimeoutError("t"), "POST", "/v2/x")
    assert synthetic_err_code(timeout_error) == -1
    assert synthetic_err_code(QQGroupAPI._error_from_exception(ConnectionError("reset"), "POST", "/v2/x")) == -3
    server = type("ServerError", (Exception,), {})("boom")
    assert synthetic_err_code(QQGroupAPI._error_from_exception(server, "POST", "/v2/x")) == -2

def test_event_row_carries_fallback_index(tmp_path):
    """备用模型实际生效的下标必须落库（ruff F841 正是抓到这里漏接线）。"""

    async def scenario():
        store = await _store(tmp_path)
        await store.insert_event(
            group_id="g1",
            verdict="allow",
            category="无",
            severity=1,
            confidence=0.9,
            stage="judged",
            prompt_version="2026-10-16-b1",
            fallback_index=2,
        )
        await store.close()
        conn = sqlite3.connect(tmp_path / "audit.db")
        row = conn.execute("select stage, prompt_version, fallback_index from mod_events").fetchone()
        conn.close()
        return row

    assert run(scenario()) == ("judged", "2026-10-16-b1", 2)


def test_manual_tool_mute_links_event_id():
    "AI 工具（handle_manual）路径也必须把 event_id 写进禁言台账："
    "生产上 27/27 禁言 source=llm_tool 且 event_id 为空，就是这条链路漏的。"
    store, _api, audit, actions, config = make_env(capabilities=(CAP_RECALL, CAP_MUTE), dry_run=False)
    settings = dict(store.settings())
    # AI 工具在宽松模式下会把禁言降级成警告（无显式授权）；这里按生产实际的 standard 模式验证真禁言
    settings["mode"] = "standard"
    run(
        actions.handle_manual(
            action="mute",
            group_id="g1",
            config=config,
            settings=settings,
            sender_openid="u1",
            sender_name="某人",
            seconds=600,
            umo="",
            source="llm_tool",
            reason="AI 工具请求禁言",
            dedupe_key="ai:mute:u1",
        )
    )
    row = audit.mutes.get(("g1", "u1"))
    assert row, "工具路径必须写禁言台账"
    assert row.get("event_id"), "工具路径必须带 event_id"
    assert row.get("source") == "llm_tool"
