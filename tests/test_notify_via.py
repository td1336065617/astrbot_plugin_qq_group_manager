"""0.15.28：通知来源标注（via）—— 只增可见性，不改任何处置行为。"""
from __future__ import annotations

import asyncio
import pathlib

from src.actions import VIA_LABELS, ActionExecutor
from src.models import GroupConfig, Verdict, default_settings

ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def v(verdict: str, category: str, severity: int, confidence: float = 0.95, stage: str = "judged") -> Verdict:
    return Verdict(
        verdict=verdict,
        category=category,
        severity=severity,
        confidence=confidence,
        reason="原判定理由",
        stage=stage,
    )


def make() -> ActionExecutor:
    return ActionExecutor(api=_Api(), store=None, audit=_Audit())


def test_via_matrix_for_violation() -> None:
    decision = make().decide(verdict=v("violation", "广告引流", 5), settings=default_settings())
    assert decision.actions == ["recall", "mute", "report"]
    assert decision.via == "matrix"


def test_via_review_for_review_above_threshold() -> None:
    decision = make().decide(verdict=v("review", "色情低俗", 2), settings=default_settings())
    assert decision.actions == ["report"]
    assert decision.via == "review"


def test_via_low_confidence_cap() -> None:
    settings = default_settings()
    settings["llm_min_confidence"] = 0.7
    decision = make().decide(
        verdict=v("violation", "广告引流", 4, confidence=0.3), settings=settings
    )
    assert decision.actions == ["report"]
    assert decision.via == "low_confidence"


def test_via_hard_rule_actions() -> None:
    decision = make().decide(
        verdict=v("violation", "广告引流", 4), settings=default_settings(), hard_actions=["mute", "report"]
    )
    assert decision.actions == ["mute", "report"]
    assert decision.via == "hard_rule"


def test_via_boundary_when_out_of_range() -> None:
    decision = make().decide(verdict=v("violation", "政治敏感", 3), settings=default_settings())
    assert decision.manual is True and decision.code == "max0"
    assert decision.via == "boundary"
    assert decision.actions == []


def test_no_actions_means_empty_via() -> None:
    decision = make().decide(verdict=v("review", "无", 1), settings=default_settings())
    assert decision.actions == []
    assert decision.via == ""


def test_via_labels_cover_all_sources() -> None:
    for code in ("matrix", "hard_rule", "review", "low_confidence", "boundary"):
        assert VIA_LABELS.get(code), f"缺少来源标签：{code}"


def test_report_payload_carries_via() -> None:
    ex = make()
    notices: list[tuple] = []

    async def notifier(kind, payload):
        notices.append((kind, payload))

    ex.notifier = notifier
    result = run(
        ex._execute(
            action="report",
            group_id="g1",
            config=GroupConfig(group_id="g1", name="群"),
            settings=default_settings(),
            verdict=v("review", "色情低俗", 2),
            msg_id="m1",
            sender_openid="o1",
            sender_name="某人",
            multiplier=1,
            send=None,
            umo="",
            via="review",
        )
    )
    assert result.ok is True
    assert notices and notices[0][0] == "moderation"
    assert notices[0][1]["via"] == "review"


def test_render_notification_shows_via_label() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "触发来源：{via}" in source, "内容审核告警文案必须显示触发来源"
    assert 'via=VIA_LABELS.get(' in source, "文案必须把 via 映射成人类可读标签"


def test_via_matches_plan_actions() -> None:
    """via 只是标注，动作集合必须与 plan_actions 完全一致（零行为变更）。"""
    ex = make()
    settings = default_settings()
    cases = [
        v("violation", "广告引流", 5),
        v("review", "色情低俗", 2),
        v("review", "无", 1),
        v("violation", "政治敏感", 3),
    ]
    for verdict in cases:
        decision = ex.decide(verdict=verdict, settings=settings)
        assert decision.actions == ex.plan_actions(verdict=verdict, settings=settings, targets=None) or decision.manual
