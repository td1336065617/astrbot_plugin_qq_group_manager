"""越界处置在 handle 侧的副作用（stage / reason 前缀 / 通知 / lenient / judged_kv）。"""
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

    async def insert_event(self, **kwargs):
        self.events.append(kwargs)
        return len(self.events)

    async def record_action(self, **kwargs):
        return None


def violation(category: str, severity: int, stage: str = "judged") -> Verdict:
    return Verdict(
        verdict="violation",
        category=category,
        severity=severity,
        confidence=0.95,
        reason="原判定理由",
        stage=stage,
    )


def env(mode: str = "standard"):
    audit = _Audit()
    ex = ActionExecutor(api=_Api(), store=None, audit=audit)
    notices: list[tuple] = []

    async def notifier(kind, payload):
        notices.append((kind, payload))

    ex.notifier = notifier
    settings = dict(default_settings())
    settings["mode"] = mode
    config = GroupConfig(group_id="g1", name="测试群", mode=mode)
    return ex, audit, notices, settings, config


def test_above_max_writes_manual_stage_and_notifies():
    ex, audit, notices, settings, config = env()
    run(ex.handle(group_id="g1", config=config, settings=settings,
                  verdict=violation("其他", 5), send=None))
    assert audit.events[-1]["stage"] == "manual"
    assert str(audit.events[-1]["reason"]).startswith("boundary:above")
    assert len(notices) == 1 and notices[0][1]["code"] == "boundary:above"


def test_silent_does_not_notify_but_still_audits():
    ex, audit, notices, settings, config = env()
    settings["boundary_action"] = {"below": "silent", "above": "silent"}
    run(ex.handle(group_id="g1", config=config, settings=settings,
                  verdict=violation("其他", 5), send=None))
    assert notices == []
    assert audit.events[-1]["stage"] == "manual"


def test_judged_kv_stays_silent_even_if_category_out_of_range():
    ex, audit, notices, settings, config = env()
    run(ex.handle(group_id="g1", config=config, settings=settings,
                  verdict=violation("政治敏感", 3, stage="judged_kv"),
                  stage="judged_kv", send=None))
    assert notices == []
    assert audit.events[-1]["stage"] == "judged_kv"


def test_lenient_does_not_add_warn_when_manual():
    ex, audit, _notices, settings, config = env("lenient")
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    run(ex.handle(group_id="g1", config=config, settings=settings,
                  verdict=violation("其他", 5), send=send))
    assert sent == []
    assert audit.events[-1]["stage"] == "manual"


def test_insult_without_target_goes_manual():
    ex, audit, notices, settings, config = env()
    run(ex.handle(group_id="g1", config=config, settings=settings,
                  verdict=violation("辱骂攻击", 4), targets={"mentions": []}, send=None))
    assert notices[0][1]["code"] == "no_target"
    assert audit.events[-1]["stage"] == "manual"


def test_in_range_still_punishes():
    ex, audit, notices, settings, config = env()
    run(ex.handle(group_id="g1", config=config, settings=settings,
                  verdict=violation("其他", 2), send=None))
    assert notices == []
    assert audit.events[-1]["stage"] == "judged"

def test_low_confidence_crossing_upper_bound_only_goes_manual():
    """#13：低置信 × 越界 ⇒ 只走越界出口（群里不补 warn，仍通知管理员）。"""
    ex, audit, notices, settings, config = env()
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    verdict = violation("其他", 5)
    verdict.confidence = 0.3
    run(ex.handle(group_id="g1", config=config, settings=settings, verdict=verdict, send=send))
    assert sent == []
    assert audit.events[-1]["stage"] == "manual"
    assert len(notices) == 1


def test_parse_error_and_illegal_value_produce_no_actions():
    """#15：parse_error / illegal_value 走既有短路，不产生动作、不改 stage、不通知。"""
    ex, audit, notices, settings, config = env()
    for field in ("parse_error",):
        verdict = violation("其他", 5)
        setattr(verdict, field, True)
        run(ex.handle(group_id="g1", config=config, settings=settings, verdict=verdict, send=None))
        assert notices == []
        assert audit.events[-1]["stage"] == "judged"
    ex2, _audit2, notices2, settings2, config2 = env()
    run(
        ex2.handle(
            group_id="g1",
            config=config2,
            settings=settings2,
            verdict=violation("其他", 5),
            stage="illegal_value",
            send=None,
        )
    )
    assert notices2 == []


def test_decide_is_pure():
    """#18：decide() 全程零次 audit / notifier 调用（纯函数）。"""
    ex, audit, notices, settings, config = env()
    assert config.group_id == "g1"
    decision = ex.decide(verdict=violation("其他", 5), settings=settings)
    assert decision.manual is True
    assert decision.code == "boundary:above"
    assert decision.actions == []
    assert audit.events == []
    assert notices == []

    auto = ex.decide(verdict=violation("其他", 2), settings=settings)
    assert auto.manual is False
    assert auto.code == ""
    assert audit.events == []
    assert notices == []
