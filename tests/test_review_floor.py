"""T4b：动作地板（review 至少 warn/report）与副作用隔离测试。"""

from __future__ import annotations

import asyncio

from src.actions import ActionExecutor
from src.models import B_TRACK_CATEGORIES, GroupConfig, Verdict, default_settings
from src.store import normalize_settings


def run(coro):
    return asyncio.run(coro)




MATRIX = {"action_matrix": {"violation": {"4": ["warn", "recall"]}}}


def executor() -> ActionExecutor:
    return ActionExecutor(api=None, store=None)


def review_verdict(stage: str = "judged", category: str = "色情低俗") -> Verdict:
    return Verdict(verdict="review", category=category, severity=2, confidence=0.9, stage=stage)


def test_floor_is_off_by_default():
    settings = {**MATRIX, **dict(default_settings())}
    assert executor().plan_actions(verdict=review_verdict(), settings=settings) == []


def test_floor_adds_warn_and_report_when_enabled():
    settings = {**dict(default_settings()), "review_floor_actions": ["warn", "report"]}
    assert executor().plan_actions(verdict=review_verdict(), settings=settings) == ["warn", "report"]


def test_floor_ignores_non_judged_stages():
    settings = {**dict(default_settings()), "review_floor_actions": ["warn"]}
    for stage in ("api_error", "low_confidence", "judged_kv", "illegal_value"):
        assert executor().plan_actions(verdict=review_verdict(stage=stage), settings=settings) == []


def test_floor_category_filter():
    settings = {**dict(default_settings()), "review_floor_actions": ["warn"], "review_floor_categories": ["辱骂攻击"]}
    assert executor().plan_actions(verdict=review_verdict(category="辱骂攻击"), settings=settings) == ["warn"]
    assert executor().plan_actions(verdict=review_verdict(category="色情低俗"), settings=settings) == []


def test_floor_does_not_override_hard_actions():
    settings = {**dict(default_settings()), "review_floor_actions": ["report"]}
    assert executor().plan_actions(verdict=review_verdict(), settings=settings, hard_actions=["warn"]) == ["warn", "report"]


def test_normalize_rejects_dangerous_floor_actions_and_categories():
    normalized = normalize_settings({
        "review_floor_actions": ["warn", "recall", "mute", "blacklist", "report", ""],
        "review_floor_categories": ["色情低俗", "不存在的品类"],
    })
    assert normalized["review_floor_actions"] == ["warn", "report"]
    assert normalized["review_floor_categories"] == ["色情低俗"]
    assert set(normalized["review_floor_categories"]) <= set(B_TRACK_CATEGORIES)


def test_multiplier_ignores_floor_driven_review_actions():
    ex = executor()
    ex._repeat_multiplier = lambda *a, **k: 3  # type: ignore[method-assign]
    review = review_verdict()
    violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.9)
    assert ex._multiplier_for(review, ["warn"], "g1", "u1", "m1", True) == 1
    assert ex._multiplier_for(violation, [], "g1", "u1", "m1") == 1
    assert ex._multiplier_for(violation, ["mute"], "g1", "u1", "m1") == 3

def test_panel_payload_includes_floor_keys():
    """第三道闸：app.js 的保存载荷必须显式带上这两个键，否则开关改了也不生效。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    app_js = (root / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    assert "review_floor_actions:" in app_js
    assert "review_floor_categories:" in app_js

def test_default_settings_never_produce_actions_for_review_or_allow():
    """默认关时的等价性：review/allow 不产生任何动作（含 judged_kv 的 violation）。"""
    settings = dict(default_settings())
    ex = executor()
    assert ex.plan_actions(verdict=review_verdict(), settings=settings) == []
    assert ex.plan_actions(verdict=review_verdict(stage="judged_kv"), settings=settings) == []
    assert ex.plan_actions(verdict=Verdict(verdict="allow", category="无"), settings=settings) == []
    kv_violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.99, stage="judged_kv")
    assert ex.plan_actions(verdict=kv_violation, settings=settings) == []

def test_floor_whitelist_is_enforced_inside_plan_actions():
    """绕过 normalize 直接把危险动作塞进设置：plan_actions 层也必须剔除。"""
    settings = {**dict(default_settings()), "review_floor_actions": ["mute", "recall", "warn"]}
    assert executor().plan_actions(verdict=review_verdict(), settings=settings) == ["warn"]


def test_lenient_mode_does_not_act_on_judged_kv_violation():
    """lenient 会给 violation 补 warn，但 judged_kv（KV 兜底）必须例外。"""
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    class _Api:
        def dry_run(self) -> bool:
            return False

    settings = dict(default_settings())
    settings.update({"mode": "lenient", "action_matrix": {"violation": {"4": ["warn", "recall"]}}})
    config = GroupConfig(group_id="g1", name="测试群", mode="lenient")
    ex = ActionExecutor(api=_Api(), store=None)

    kv_violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.99, stage="judged_kv")
    run(ex.handle(group_id="g1", config=config, verdict=kv_violation, settings=settings, send=send))
    assert sent == []

    judged_violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.99, stage="judged")
    run(ex.handle(group_id="g1", config=config, verdict=judged_violation, settings=settings, send=send))
    assert sent, "正常 judged 的 violation 在 lenient 下仍应补一条 warn"
