"""跨群黑名单（B5）与二维码内容识别（B4）测试。"""
from __future__ import annotations

import asyncio

from src.links import qr_risk_text
from src.models import Verdict
from src.moderator import parse_verdict


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.data[key] = value


def _store():
    from src.store import PluginStore

    return PluginStore(FakeKV())


# ----------------------------------------------------------------------
# B4 二维码内容识别
# ----------------------------------------------------------------------


def test_qr_risk_text_detects_group_number_and_links():
    assert qr_risk_text("加群 123456789") == "二维码含群号"
    assert qr_risk_text("https://t.cn/abc123") == "二维码含短链"
    assert qr_risk_text("https://example.com/invite") == "二维码含链接"
    assert qr_risk_text("") == ""
    assert qr_risk_text("hello world") == ""


def test_parse_verdict_reads_qr_text():
    verdict = parse_verdict(
        '{"verdict":"review","category":"广告","severity":2,"confidence":0.8,'
        '"reason":"疑似引流","suggested_action":"warn","qr_text":"加群 987654321"}'
    )
    assert verdict.qr_text == "加群 987654321"
    assert qr_risk_text(verdict.qr_text) == "二维码含群号"


def test_parse_verdict_without_qr_text_keeps_empty():
    verdict = parse_verdict(
        '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,'
        '"reason":"正常","suggested_action":"none"}'
    )
    assert verdict.qr_text == ""
    assert qr_risk_text(verdict.qr_text) == ""


def test_verdict_clamped_keeps_qr_text_but_truncates():
    verdict = Verdict(verdict="review", qr_text="x" * 500).clamped()
    assert len(verdict.qr_text) == 300


# ----------------------------------------------------------------------
# B5 跨群黑名单
# ----------------------------------------------------------------------


def test_global_blacklist_add_remove_and_reason():
    async def scenario():
        store = _store()
        await store.load()
        assert store.is_globally_blacklisted("u_1") is False
        await store.add_global_blacklist("u_1", reason="发广告", added_by="webui:admin")
        assert store.is_globally_blacklisted("u_1") is True
        assert store.global_blacklist_reason("u_1") == "发广告"
        snapshot = store.global_blacklist()
        assert snapshot["u_1"]["added_by"] == "webui:admin"
        assert await store.remove_global_blacklist("u_1") is True
        assert store.is_globally_blacklisted("u_1") is False
        # 重复移除返回 False
        assert await store.remove_global_blacklist("u_1") is False

    asyncio.run(scenario())


def test_global_blacklist_persists_to_kv():
    async def scenario():
        kv = FakeKV()
        from src.store import PluginStore

        store = PluginStore(kv)
        await store.load()
        await store.add_global_blacklist("u_2", reason="诈骗", added_by="qq:admin")
        restored = PluginStore(kv)
        await restored.load()
        assert restored.is_globally_blacklisted("u_2") is True
        assert restored.global_blacklist_reason("u_2") == "诈骗"

    asyncio.run(scenario())


def test_join_review_rejects_globally_blacklisted():
    async def scenario():
        from src.join_review import JoinReviewer

        store = _store()
        await store.load()
        await store.add_global_blacklist("u_3", reason="跨群广告")
        reviewer = JoinReviewer(api=None, store=store)
        decision = await reviewer.judge(
            "g1",
            {"member_openid": "u_3", "username": "广告号", "risk_tips": "无"},
            mode="standard",
        )
        assert decision.op == "decline"
        assert decision.auto is True
        assert "跨群黑名单" in decision.reason

    asyncio.run(scenario())


def test_join_review_without_global_blacklist_is_unaffected():
    async def scenario():
        from src.join_review import JoinReviewer

        store = _store()
        await store.load()
        reviewer = JoinReviewer(api=None, store=store)
        # 未命中任何硬规则时会走到 LLM 分支（judge_call 未提供 → 转人工）
        decision = await reviewer.judge(
            "g1",
            {"member_openid": "u_4", "username": "普通用户", "risk_tips": "无"},
            mode="standard",
        )
        assert "跨群黑名单" not in (decision.reason or "")

    asyncio.run(scenario())
