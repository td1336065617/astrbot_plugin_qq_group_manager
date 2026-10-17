"""显式入口（AI 写工具）的幂等 / 模式降级用例——N3 的回归测试。"""

from __future__ import annotations

import asyncio

from fakes import FakeAudit

from src.actions import ActionExecutor
from src.models import default_settings


class FakeApi:
    def __init__(self, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self.calls: list = []

    def dry_run(self) -> bool:
        return self._dry_run

    async def mute_member(self, group_id, member_openid, *, seconds, caller="moderation"):
        self.calls.append(("mute", member_openid, seconds, caller))
        return {"_dry_run": self._dry_run} if self._dry_run else {}

    async def unmute_member(self, group_id, member_openid, *, caller="moderation"):
        self.calls.append(("unmute", member_openid, caller))
        return {"_dry_run": self._dry_run} if self._dry_run else {}


class FakeConfig:
    def __init__(self, mode: str = "") -> None:
        self.name = "测试群"
        self.mode = mode

    def capability_ok(self, capability: str) -> bool:
        return True


class FakeStore:
    def settings(self) -> dict:
        return dict(default_settings())


def run(coro):
    return asyncio.run(coro)


def standard_settings() -> dict:
    """显式 standard 模式：默认配置是 lenient（会按设计把 mute 降级为 warn）。"""
    settings = default_settings()
    settings["mode"] = "standard"
    return settings

def make_executor(dry_run: bool = False):
    api = FakeApi(dry_run)
    executor = ActionExecutor(api=api, store=FakeStore(), audit=FakeAudit(), logger=None)
    return executor, api


def test_two_different_targets_both_execute() -> None:
    """N3 回归：同群 1 小时内，两个不同目标必须各自生效（幂等键要含目标）。"""
    executor, api = make_executor()
    settings = standard_settings()

    async def scenario():
        first = await executor.handle_manual(
            action="mute", group_id="g1", config=FakeConfig(), settings=settings,
            sender_openid="u_A", sender_name="A", seconds=600, source="llm_tool",
            dedupe_key="ai:mute:u_A",
        )
        second = await executor.handle_manual(
            action="mute", group_id="g1", config=FakeConfig(), settings=settings,
            sender_openid="u_B", sender_name="B", seconds=600, source="llm_tool",
            dedupe_key="ai:mute:u_B",
        )
        return first, second

    first, second = run(scenario())
    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert len(api.calls) == 2, api.calls
    assert {call[1] for call in api.calls} == {"u_A", "u_B"}


def test_same_target_is_deduped() -> None:
    executor, api = make_executor()
    settings = standard_settings()

    async def scenario():
        kwargs = dict(
            action="mute", group_id="g1", config=FakeConfig(), settings=settings,
            sender_openid="u_A", sender_name="A", seconds=600, source="llm_tool",
            dedupe_key="ai:mute:u_A",
        )
        return await executor.handle_manual(**kwargs), await executor.handle_manual(**kwargs)

    first, second = run(scenario())
    assert first["ok"] is True
    assert second["ok"] is True and "重复" in str(second["message"])
    assert len(api.calls) == 1


def test_dry_run_does_not_consume_idempotency() -> None:
    """演练不落幂等键：dry-run 之后真实执行仍要能生效。"""
    executor, api = make_executor(dry_run=True)
    settings = standard_settings()

    async def scenario():
        kwargs = dict(
            action="mute", group_id="g1", config=FakeConfig(), settings=settings,
            sender_openid="u_A", sender_name="A", seconds=600, source="llm_tool",
            dedupe_key="ai:mute:u_A",
        )
        first = await executor.handle_manual(**kwargs)
        executor.api._dry_run = False
        second = await executor.handle_manual(**kwargs)
        return first, second

    first, second = run(scenario())
    assert first["dry_run"] is True
    assert second["dry_run"] is False and second["ok"] is True
    assert len(api.calls) == 2


def test_lenient_downgrades_mute_to_warn() -> None:
    executor, api = make_executor()
    settings = standard_settings()
    settings["dry_run_warn"] = True

    async def scenario():
        return await executor.handle_manual(
            action="mute", group_id="g1", config=FakeConfig(mode="lenient"), settings=settings,
            sender_openid="u_A", sender_name="A", seconds=600, source="llm_tool",
            dedupe_key="ai:mute:u_A",
        )

    result = run(scenario())
    assert result["action"] == "warn", result
    assert result["downgraded_to"] == "warn"
    assert api.calls == [], "lenient 下不得真的禁言"


def test_log_only_skips_action() -> None:
    executor, api = make_executor()

    async def scenario():
        return await executor.handle_manual(
            action="mute", group_id="g1", config=FakeConfig(mode="log_only"), settings=standard_settings(),
            sender_openid="u_A", sender_name="A", seconds=600, source="llm_tool",
        )

    result = run(scenario())
    assert result["ok"] is True, "仅记录是策略结果，不是能力错误"
    assert result["executed"] is False
    assert result["mode"] == "log_only"
    assert api.calls == []


def test_destructive_actions_are_rejected() -> None:
    executor, api = make_executor()

    async def scenario():
        return await executor.handle_manual(
            action="recall", group_id="g1", config=FakeConfig(), settings=standard_settings(),
            sender_openid="u_A", source="llm_tool",
        )

    result = run(scenario())
    assert result["ok"] is False
    assert "recall" not in result["allowed"]
    assert api.calls == []


def test_minutes_are_clamped_by_max_mute_days() -> None:
    executor, api = make_executor()
    settings = standard_settings()
    settings["max_mute_days"] = 1

    async def scenario():
        return await executor.handle_manual(
            action="mute", group_id="g1", config=FakeConfig(), settings=settings,
            sender_openid="u_A", sender_name="A", seconds=30 * 86400, source="llm_tool",
        )

    run(scenario())
    assert api.calls[0][2] == 86400, api.calls   # 30 天被 max_mute_days=1 钳到 1 天


def test_lenient_warn_without_channel_is_policy_result() -> None:
    """lenient 降级到 warn 但发不出提醒时：按"策略结果"回报，不是能力错误。"""
    executor, api = make_executor()
    settings = standard_settings()

    async def scenario():
        return await executor.handle_manual(
            action="mute",
            group_id="g1",
            config=FakeConfig(mode="lenient"),
            settings=settings,
            sender_openid="u_A",
            sender_name="A",
            seconds=600,
            source="llm_tool",
            dedupe_key="ai:mute:u_A",
        )

    result = run(scenario())
    assert result["ok"] is True, result
    assert result["executed"] is False
    assert result["downgraded_to"] == "warn"
    assert api.calls == [], "lenient 下不得真的禁言"
