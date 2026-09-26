"""头像多模态复核与入群 LLM 闸门测试。"""

from __future__ import annotations

import asyncio

from src.api_client import QQGroupAPI
from src.join_review import JoinReviewer
from src.store import PluginStore
from tests.fakes import FakeAudit, FakeKV, FakeTransport

LIST_PATH = "/v2/groups/{group_openid}/join_request_list"
APPROVE_PATH = "/v2/groups/{group_openid}/approval_join_request/{member_openid}"
AVATAR = "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"


def run(coro):
    return asyncio.run(coro)


def avatar_profile(**overrides):
    base = {
        "kind": "onebot",
        "nickname": "小号",
        "qq_level": 30,
        "account_age_days": 100,
        "qid": "q-1",
        "avatar_url": AVATAR,
        "source": "onebot_stranger",
        "degraded": False,
    }
    base.update(overrides)
    return base


def make_reviewer(responses, *, settings=None, errors=None, legacy=False):
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.ensure_group("g1", name="测试群"))
    if settings:
        run(store.update_settings(settings))
    api = QQGroupAPI(
        FakeTransport(
            {
                ("GET", LIST_PATH): {"list": [], "next_cursor": ""},
                ("POST", APPROVE_PATH): {},
            }
        )
    )
    calls: list[dict] = []

    if legacy:

        async def judge_call(system_prompt, user_prompt):
            calls.append({"system": system_prompt, "images": []})
            return responses[0]

    else:

        async def judge_call(system_prompt, user_prompt, image_urls=None):
            index = len(calls)
            calls.append({"system": system_prompt, "images": list(image_urls or [])})
            boom = (errors or {}).get(index)
            if boom is not None:
                raise boom
            return responses[min(index, len(responses) - 1)]

    reviewer = JoinReviewer(
        api=api, store=store, audit=FakeAudit(), judge_call=judge_call
    )
    return store, reviewer, calls


def request_payload(**overrides):
    payload = {
        "join_request_id": "j1",
        "member_openid": "u1",
        "username": "张三",
        "apply_source": "self_apply",
        "risk_tips": "",
        "bot": False,
        "verify_info": {"method": "verify_message", "verify_message": "你好"},
        "profile": avatar_profile(),
    }
    payload.update(overrides)
    return payload


def test_avatar_off_never_sends_image():
    _, reviewer, calls = make_reviewer(['{"decision":"approve","confidence":0.99,"reason":"ok"}'])
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert decision.op == "approve"
    assert calls[0]["images"] == []
    assert len(calls) == 1


def test_avatar_always_attaches_image_to_first_call():
    _, reviewer, calls = make_reviewer(
        ['{"decision":"approve","confidence":0.99,"reason":"ok"}'],
        settings={"join_avatar_review": "always"},
    )
    run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert calls[0]["images"] == [AVATAR]
    assert len(calls) == 1


def test_approve_only_triggers_second_call_and_declines():
    _, reviewer, calls = make_reviewer(
        [
            '{"decision":"approve","confidence":0.85,"reason":"信息正常"}',
            '{"risk":true,"confidence":0.93,"reason":"头像为擦边图"}',
        ],
        settings={"join_avatar_review": "approve_only"},
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert len(calls) == 2
    assert calls[0]["images"] == []
    assert calls[1]["images"] == [AVATAR]
    assert decision.op == "decline" and decision.auto
    assert decision.gate == "avatar"
    assert decision.blacklist is True


def test_approve_only_skips_when_confident_enough():
    _, reviewer, calls = make_reviewer(
        ['{"decision":"approve","confidence":0.99,"reason":"信息正常"}'],
        settings={"join_avatar_review": "approve_only"},
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert len(calls) == 1
    assert decision.op == "approve"


def test_avatar_not_risky_keeps_approve():
    _, reviewer, calls = make_reviewer(
        [
            '{"decision":"approve","confidence":0.85,"reason":"信息正常"}',
            '{"risk":false,"confidence":0.2,"reason":"普通头像"}',
        ],
        settings={"join_avatar_review": "approve_only"},
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert len(calls) == 2
    assert decision.op == "approve" and decision.gate == ""


def test_avatar_low_confidence_keeps_approve():
    _, reviewer, _calls = make_reviewer(
        [
            '{"decision":"approve","confidence":0.85,"reason":"信息正常"}',
            '{"risk":true,"confidence":0.3,"reason":"拿不准"}',
        ],
        settings={"join_avatar_review": "approve_only"},
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert decision.op == "approve"


def test_avatar_failure_keeps_original_decision():
    _, reviewer, calls = make_reviewer(
        [
            '{"decision":"approve","confidence":0.85,"reason":"信息正常"}',
            '{"risk":true,"confidence":0.99,"reason":"擦边"}',
        ],
        settings={"join_avatar_review": "approve_only"},
        errors={1: RuntimeError("model down")},
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert len(calls) == 2
    assert decision.op == "approve" and decision.gate == ""


def test_avatar_never_upgrades_a_decline():
    _, reviewer, calls = make_reviewer(
        ['{"decision":"decline","confidence":0.95,"reason":"广告"}'],
        settings={"join_avatar_review": "approve_only"},
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert decision.op == "decline"
    assert len(calls) == 1


def test_avatar_skipped_without_avatar_url():
    _, reviewer, calls = make_reviewer(
        ['{"decision":"approve","confidence":0.85,"reason":"信息正常"}'],
        settings={"join_avatar_review": "approve_only"},
    )
    request = request_payload(profile=avatar_profile(avatar_url=""))
    decision = run(reviewer.judge("g1", request, mode="standard"))
    assert decision.op == "approve"
    assert len(calls) == 1


def test_legacy_two_arg_judge_call_still_works():
    _, reviewer, calls = make_reviewer(
        ['{"decision":"approve","confidence":0.99,"reason":"ok"}'],
        settings={"join_avatar_review": "always"},
        legacy=True,
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert decision.op == "approve"
    assert len(calls) == 1
