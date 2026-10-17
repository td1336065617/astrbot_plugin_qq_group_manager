"""判定口径与处罚边界：类别上下限 + 越界流向（纯逻辑）。"""
from __future__ import annotations

from src.models import (
    CATEGORIES,
    Verdict,
    default_settings,
    evaluate_boundary,
    has_target,
    resolve_boundary_action,
    resolve_category_policy,
)


def violation(category: str, severity: int, stage: str = "judged") -> Verdict:
    return Verdict(
        verdict="violation",
        category=category,
        severity=severity,
        confidence=0.9,
        reason="r",
        stage=stage,
    )


def test_defaults_cover_every_category() -> None:
    policy = resolve_category_policy(default_settings())
    assert set(policy) == set(CATEGORIES)
    assert len(policy) == 9


def test_default_bounds() -> None:
    policy = resolve_category_policy(default_settings())
    assert policy["其他"]["max"] == 2
    assert policy["无"]["max"] == 2
    assert policy["政治敏感"]["max"] == 0
    assert policy["刷屏灌水"]["max"] == 3
    for category in ("广告引流", "色情低俗", "辱骂攻击", "违法违规", "诈骗赌博"):
        assert policy[category]["max"] is None
        assert policy[category]["min"] is None


def test_dirty_values_fall_back_to_default() -> None:
    settings = {
        "category_policy": {
            "其他": {"min": True, "max": 9},
            "无": {"min": 4, "max": 1},
            "未知类": {"min": 1, "max": 1},
        }
    }
    policy = resolve_category_policy(settings)
    assert policy["其他"] == {"min": None, "max": 2}
    assert policy["无"] == {"min": None, "max": 2}
    assert len(policy) == 9


def test_non_dict_policy_uses_defaults() -> None:
    assert resolve_category_policy({"category_policy": "x"}) == resolve_category_policy({})


def test_boundary_action_defaults_and_validation() -> None:
    assert resolve_boundary_action({}) == {"below": "notify", "above": "notify"}
    silent = resolve_boundary_action({"boundary_action": {"above": "silent"}})
    assert silent == {"below": "notify", "above": "silent"}
    bad = resolve_boundary_action({"boundary_action": {"above": "boom"}})
    assert bad["above"] == "notify"


def test_in_range_is_auto() -> None:
    settings = default_settings()
    assert evaluate_boundary(violation("其他", 2), settings, None) == (False, "", True)
    assert evaluate_boundary(violation("广告引流", 5), settings, None) == (False, "", True)


def test_above_max_goes_manual() -> None:
    settings = default_settings()
    assert evaluate_boundary(violation("其他", 5), settings, None) == (True, "boundary:above", True)
    assert evaluate_boundary(violation("刷屏灌水", 4), settings, None)[1] == "boundary:above"


def test_political_always_manual() -> None:
    settings = default_settings()
    for severity in (1, 3, 5):
        assert evaluate_boundary(violation("政治敏感", severity), settings, None) == (True, "max0", True)


def test_below_min_goes_manual() -> None:
    settings = {"category_policy": {"色情低俗": {"min": 3, "max": None}}}
    assert evaluate_boundary(violation("色情低俗", 1), settings, None) == (True, "boundary:below", True)


def test_silent_disables_notification() -> None:
    settings = dict(default_settings())
    settings["boundary_action"] = {"below": "silent", "above": "silent"}
    assert evaluate_boundary(violation("其他", 5), settings, None) == (True, "boundary:above", False)
    assert evaluate_boundary(violation("政治敏感", 2), settings, None) == (True, "max0", False)


def test_insult_requires_target() -> None:
    settings = default_settings()
    empty = evaluate_boundary(violation("辱骂攻击", 4), settings, {"mentions": []})
    assert empty == (True, "no_target", True)
    assert evaluate_boundary(violation("辱骂攻击", 4), settings, {"mentions": ["u1"]})[0] is False
    assert evaluate_boundary(violation("辱骂攻击", 4), settings, {"reply_sender": "u2"})[0] is False
    assert evaluate_boundary(violation("辱骂攻击", 4), settings, {"named": True})[0] is False


def test_unknown_targets_skip_object_check() -> None:
    assert has_target(None) is True
    assert has_target({}) is False
    assert evaluate_boundary(violation("辱骂攻击", 4), default_settings(), None)[0] is False


def test_non_violation_passes_through() -> None:
    allow = Verdict(verdict="allow", category="其他", severity=0, confidence=0.9, reason="")
    assert evaluate_boundary(allow, default_settings(), None) == (False, "", True)
