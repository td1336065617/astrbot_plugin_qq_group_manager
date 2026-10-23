"""0.15.30：低置信封顶改为「只上报」，群里不再发警告。"""
from __future__ import annotations

import asyncio

from src.actions import ActionExecutor
from src.models import GroupConfig, Verdict, default_settings


def run(coro):
    return asyncio.run(coro)


class _Api:
    def dry_run(self) -> bool:
        return False


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.actions: list[dict] = []

    async def insert_event(self, **kwargs):
        self.events.append(kwargs)
        return 1

    def record_action(self, **kwargs):  # 生产 AuditStore.record_action 是同步入队的
        self.actions.append(kwargs)


def make(settings=None):
    audit = _Audit()
    ex = ActionExecutor(api=_Api(), store=None, audit=audit)
    s = dict(default_settings())
    s["mode"] = "standard"  # 默认是 lenient，会强制补 warn，掩盖本用例
    s["llm_min_confidence"] = 0.94
    if settings:
        s.update(settings)
    return ex, audit, s


def low(conf: float = 0.80) -> Verdict:
    return Verdict(
        verdict="violation",
        category="色情低俗",
        severity=3,
        confidence=conf,
        reason="分享含nsfw关键词的小程序",
        stage="judged",
        source="llm",
    )


def test_plan_is_report_only() -> None:
    ex, _audit, settings = make()
    assert ex.plan_actions(verdict=low(), settings=settings) == ["report"]


def test_high_confidence_still_uses_matrix() -> None:
    ex, _audit, settings = make()
    assert ex.plan_actions(verdict=low(0.95), settings=settings) == ["warn", "mute"]


def test_handle_does_not_warn_in_group() -> None:
    ex, audit, settings = make()
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    async def notifier(kind, payload):
        return False, "未配置通知会话"

    ex.notifier = notifier
    run(
        ex.handle(
            group_id="g1",
            config=GroupConfig(group_id="g1", name="群"),
            verdict=low(),
            settings=settings,
            msg_id="m1",
            sender_openid="u1",
            sender_name="某人",
            send=send,
        )
    )
    assert sent == [], "低置信不应在群内发警告（0.15.30 起）"
    assert [row["action"] for row in audit.actions] == ["report"]
    assert audit.events[0]["verdict"] == "violation", "判定不被改写"
    assert audit.events[0]["severity"] == 3


def test_handle_keeps_report_recorded_when_channel_empty() -> None:
    ex, audit, settings = make()

    async def notifier(kind, payload):
        return False, "未配置通知会话"

    ex.notifier = notifier
    result = run(
        ex.handle(
            group_id="g1",
            config=GroupConfig(group_id="g1", name="群"),
            verdict=low(),
            settings=settings,
            msg_id="m2",
            sender_openid="u1",
            sender_name="某人",
            send=None,
        )
    )
    assert result["planned"] == ["report"]
    assert "未送达" in str(audit.actions[0]["err_msg"])


def test_dry_run_does_not_add_warn() -> None:
    """封顶后 planned 只有 report ⇒ destructive_planned 为空 ⇒ dry-run 不再补 warn。"""
    ex, audit, settings = make()
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    class _DryApi:
        def dry_run(self):
            return True

    ex.api = _DryApi()
    run(
        ex.handle(
            group_id="g1",
            config=GroupConfig(group_id="g1", name="群"),
            verdict=low(),
            settings=settings,
            msg_id="m3",
            sender_openid="u1",
            sender_name="某人",
            send=send,
        )
    )
    assert sent == []
    assert [row["action"] for row in audit.actions] == ["report"]


def test_lenient_still_warns_low_confidence() -> None:
    """已知例外：lenient 语义就是「只警告」，低置信在 lenient 下仍会补 warn（钉住，防误以为已修）。"""
    ex, audit, settings = make({"mode": "lenient"})
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    async def _n(kind, payload):
        return False, "未配置通知会话"

    ex.notifier = _n
    run(
        ex.handle(
            group_id="g1",
            config=GroupConfig(group_id="g1", name="群"),
            verdict=low(),
            settings=settings,
            msg_id="m4",
            sender_openid="u1",
            sender_name="某人",
            send=send,
        )
    )
    assert sent, "lenient 下低置信仍会补一条群内警告（唯一已知例外）"
    assert [row["action"] for row in audit.actions] == ["warn", "report"]
