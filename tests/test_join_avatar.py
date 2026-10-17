"""入群审批：低置信归宿可配（原「头像独立复核」用例已随该行为删除而重写）。"""
from __future__ import annotations

import asyncio
import json

from src.join_review import JoinReviewer

BASE = {"join_min_confidence": 0.8, "join_decline_blacklist": True}


def reviewer() -> JoinReviewer:
    return JoinReviewer(api=None, store=None, logger=None)


def raw(decision: str, confidence: float) -> str:
    return json.dumps(
        {"decision": decision, "confidence": confidence, "reason": "r", "risk": "无"}
    )


def test_low_confidence_manual_by_default() -> None:
    out = reviewer().parse_decision(raw("approve", 0.5), {}, settings=dict(BASE), mode="standard")
    assert out.op == "approve"
    assert out.auto is False
    assert out.source == "manual"


def test_low_confidence_decline_when_configured() -> None:
    settings = dict(BASE, join_low_confidence_action="decline")
    out = reviewer().parse_decision(raw("approve", 0.5), {}, settings=settings, mode="standard")
    assert out.op == "decline"
    assert out.auto is True


def test_low_confidence_pass_when_configured() -> None:
    settings = dict(BASE, join_low_confidence_action="pass")
    out = reviewer().parse_decision(raw("approve", 0.5), {}, settings=settings, mode="standard")
    assert out.op == "approve"
    assert out.auto is True


def test_strict_mode_semantics_equal_decline() -> None:
    out = reviewer().parse_decision(raw("approve", 0.5), {}, settings=dict(BASE), mode="strict")
    assert out.op == "decline"
    assert out.auto is True


def test_high_confidence_is_auto() -> None:
    out = reviewer().parse_decision(raw("decline", 0.9), {}, settings=dict(BASE), mode="standard")
    assert out.op == "decline"
    assert out.auto is True

class _Store:
    MODE = "always"

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop

    def group(self, group_id: str):
        return None

    def local_blacklist(self, group_id: str):
        return []

    def effective_join_settings(self, group_id: str):
        return {"join_avatar_review": _Store.MODE, "join_min_confidence": 0.8}


def run_main_judgement_for(mode: str) -> None:
    _Store.MODE = mode
    captured = []
    reviewer = JoinReviewer(api=None, store=_Store(), logger=None)

    async def fake(system_prompt, user_prompt, images, group_id, **kwargs):
        captured.append((list(images or []), kwargs.get("image_required")))
        return json.dumps({"decision": "approve", "confidence": 0.9, "reason": "ok", "risk": "无"})

    reviewer.judge_call = fake
    reviewer._call_judge = fake
    request = {
        "member_openid": "u1",
        "username": "n",
        "profile": {"avatar_url": "https://example.invalid/a.png"},
        "verify_info": {},
    }
    asyncio.run(reviewer.judge("g1", request, mode="standard"))
    assert captured, "主判定没有被调用"
    images, image_required = captured[0]
    assert images == ["https://example.invalid/a.png"]
    assert image_required is True


def test_avatar_attached_in_always_mode() -> None:
    run_main_judgement_for("always")


def test_avatar_attached_in_approve_only_mode() -> None:
    run_main_judgement_for("approve_only")
