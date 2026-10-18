"""0.15.29：通知留空的兼容（只想看日志）+ 上报记账要能区分"未送达"。"""
from __future__ import annotations

import asyncio
import pathlib

from src.actions import ActionExecutor
from src.models import GroupConfig, Verdict, default_settings, notify_session_state

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


class _Api:
    def dry_run(self) -> bool:
        return False


class _Audit:
    def __init__(self) -> None:
        self.actions: list[dict] = []

    async def insert_event(self, **kwargs):
        return 1

    async def record_action(self, **kwargs):
        self.actions.append(kwargs)


def v() -> Verdict:
    return Verdict(verdict="review", category="色情低俗", severity=2, confidence=0.9, reason="r", stage="judged")


def executor(notifier) -> ActionExecutor:
    ex = ActionExecutor(api=_Api(), store=None, audit=_Audit())
    ex.notifier = notifier
    return ex


def report(ex: ActionExecutor, via: str = "review"):
    return run(
        ex._execute(
            action="report",
            group_id="g1",
            config=GroupConfig(group_id="g1", name="群"),
            settings=default_settings(),
            verdict=v(),
            msg_id="m1",
            sender_openid="o1",
            sender_name="某人",
            multiplier=1,
            send=None,
            umo="",
            via=via,
        )
    )


def test_empty_session_is_enabled_false_and_valid() -> None:
    state = notify_session_state({})
    assert state["configured"] is False
    assert state["enabled"] is False
    assert state["valid"] is True, "留空不是格式错误（0.15.29 起）"
    assert "留空 = 不推送" in state["hint"]


def test_invalid_session_still_warns() -> None:
    state = notify_session_state({"notify_session": "BROKEN"})
    assert state["configured"] is True and state["valid"] is False
    assert state["hint"].startswith("⚠️")


def test_report_marks_not_delivered_and_counts_skipped() -> None:
    async def notifier(kind, payload):
        return False, "未配置通知会话"

    ex = executor(notifier)
    result = report(ex)
    assert result.ok is True
    assert "未送达" in (result.message or "")
    assert ex.stats["skipped"] >= 1
    assert ex.stats["executed"] == 0


def test_report_marks_delivered_and_counts_executed() -> None:
    async def notifier(kind, payload):
        return True, ""

    ex = executor(notifier)
    result = report(ex)
    assert result.ok is True
    assert not result.message
    assert ex.stats["executed"] == 1


def test_notifier_returning_none_is_treated_as_delivered() -> None:
    async def notifier(kind, payload):
        return None

    ex = executor(notifier)
    assert report(ex).ok is True
    assert ex.stats["executed"] == 1, "自定义/桩通知不返回元组时按已送达处理（不误报未送达）"


def test_panel_renders_hint_for_empty_session() -> None:
    js = (ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    assert "notify.enabled === false" in js, "面板必须在留空时展示中性提示"
