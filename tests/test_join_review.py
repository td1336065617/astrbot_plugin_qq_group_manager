"""JoinReviewer 单元测试：判定链、阈值、审批、去重。"""

from __future__ import annotations

import asyncio

from src.api_client import QQGroupAPI
from src.join_review import JoinReviewer
from src.store import PluginStore
from tests.fakes import FakeAudit, FakeKV, FakeTransport

LIST_PATH = "/v2/groups/{group_openid}/join_request_list"
APPROVE_PATH = "/v2/groups/{group_openid}/approval_join_request/{member_openid}"


def run(coro):
    return asyncio.run(coro)


def make_env(
    *,
    response_text='{"decision":"approve","confidence":0.9,"reason":"信息正常"}',
    transport=None,
    judge_error=None,
):
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.ensure_group("g1", name="测试群"))
    fake_transport = transport or FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [], "next_cursor": ""},
            ("POST", APPROVE_PATH): {},
        }
    )
    api = QQGroupAPI(fake_transport)
    audit = FakeAudit()
    api.audit = audit

    async def judge_call(system_prompt, user_prompt):
        assert "申请人昵称" in user_prompt
        if judge_error:
            raise judge_error
        return response_text

    reviewer = JoinReviewer(api=api, store=store, audit=audit, judge_call=judge_call)
    return store, api, audit, reviewer, fake_transport


def request_payload(**overrides):
    payload = {
        "join_request_id": "j1",
        "member_openid": "u1",
        "username": "张三",
        "apply_source": "self_apply",
        "risk_tips": "",
        "bot": False,
        "verify_info": {"method": "verify_message", "verify_message": "你好"},
    }
    payload.update(overrides)
    return payload


def test_judge_hard_rules():
    store, _, _, reviewer, _ = make_env()
    top = run(reviewer.judge("g1", request_payload(risk_tips="top_tips"), mode="standard"))
    assert top.op == "decline" and top.auto and top.blacklist

    bot = run(reviewer.judge("g1", request_payload(bot=True), mode="standard"))
    assert bot.op == "decline" and bot.auto

    run(store.update_local_blacklist("g1", ["u1"]))
    blocked = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert blocked.op == "decline" and "黑名单" in blocked.reason

    run(store.update_local_blacklist("g1", []))
    run(store.update_settings({"join_trust_inviter": True}))
    invited = run(reviewer.judge("g1", request_payload(apply_source="invited"), mode="standard"))
    assert invited.op == "approve" and invited.auto


def test_judge_llm_decisions_and_threshold():
    _, _, _, reviewer, _ = make_env(
        response_text='{"decision":"approve","confidence":0.95,"reason":"正常"}'
    )
    decision = run(reviewer.judge("g1", request_payload(), mode="standard"))
    assert decision.op == "approve" and decision.auto and decision.source == "llm"

    _, _, _, strict, _ = make_env(
        response_text='{"decision":"decline","confidence":0.95,"reason":"广告"}'
    )
    declined = run(strict.judge("g1", request_payload(), mode="standard"))
    assert declined.op == "decline" and declined.blacklist is True

    _, _, _, low, _ = make_env(
        response_text='{"decision":"approve","confidence":0.3,"reason":"不确定"}'
    )
    manual = run(low.judge("g1", request_payload(), mode="standard"))
    assert manual.auto is False and manual.source == "manual"

    strict_mode = run(low.judge("g1", request_payload(), mode="strict"))
    assert strict_mode.op == "decline" and strict_mode.auto is True


def test_judge_human_mode_and_llm_failure():
    _, _, _, reviewer, _ = make_env()
    human = run(reviewer.judge("g1", request_payload(), mode="human"))
    assert human.auto is False and human.source == "manual"

    _, _, _, broken, _ = make_env(judge_error=RuntimeError("boom"))
    result = run(broken.judge("g1", request_payload(), mode="standard"))
    assert result.auto is False and "模型调用失败" in result.reason
    assert broken.stats.failed == 1


def test_parse_decision_invalid_json():
    _, _, _, reviewer, _ = make_env()
    decision = reviewer.parse_decision("不是 JSON", request_payload(), settings={}, mode="standard")
    assert decision.auto is False and decision.source == "manual"


def test_poll_group_auto_approves_and_dedupes():
    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {
                "list": [request_payload()],
                "next_cursor": "next-1",
            },
            ("POST", APPROVE_PATH): {"trace_id": "t1"},
        }
    )
    store, _api, audit, reviewer, _ = make_env(transport=transport)
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    created = run(reviewer.poll_group("g1"))
    assert created == []  # 自动审批，不进入待审
    assert reviewer.stats.approved == 1
    assert audit.joins["j1"]["decision"] == "approve"
    assert run(store.get_join_cursor("g1")) == "next-1"

    # 再次轮询不应重复审批（数据库已记录）
    before = len(transport.calls)
    run(reviewer.poll_group("g1"))
    approve_calls = [call for call in transport.calls[before:] if call["path"] == APPROVE_PATH]
    assert approve_calls == []


def test_poll_group_keeps_pending_for_human():
    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [request_payload()], "next_cursor": ""},
            ("POST", APPROVE_PATH): {},
        }
    )
    store, _api, audit, reviewer, _ = make_env(
        transport=transport, response_text='{"decision":"approve","confidence":0.2}'
    )
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    created = run(reviewer.poll_group("g1"))
    assert len(created) == 1
    pending = reviewer.list_pending("g1")
    assert len(pending) == 1
    assert audit.joins["j1"]["decision"] == "pending"

    result = run(
        reviewer.decide_manual("g1", "u1", op="approve", join_request_id="j1", by="human:tester")
    )
    assert result["ok"] is True
    assert reviewer.list_pending("g1") == []
    assert audit.joins["j1"]["decided_by"] == "human:tester"


def test_submit_failure_is_recorded():
    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [request_payload()], "next_cursor": ""},
            ("POST", APPROVE_PATH): {"err_code": 11282, "message": "检查是否是管理员未通过"},
        }
    )
    store, _api, audit, reviewer, _ = make_env(transport=transport)
    run(store.update_group("g1", {"join_review_mode": "standard"}))
    run(reviewer.poll_group("g1"))
    assert reviewer.stats.failed == 1
    assert "11282" in (audit.joins["j1"]["reason"] or "") or "管理员" in (
        audit.joins["j1"]["reason"] or ""
    )


# --------------------------------------------------------------------------
# 申请人画像（0.12.0）
# --------------------------------------------------------------------------
def _profile(**overrides):
    base = {
        "kind": "onebot",
        "nickname": "小号",
        "qq_level": 30,
        "account_age_days": 999,
        "qid": "q-1",
        "source": "onebot_stranger",
        "degraded": False,
    }
    base.update(overrides)
    return base


def test_official_profile_does_not_trigger_missing_policy():
    _, _, _, reviewer, _ = make_env()
    request = request_payload(profile={"kind": "official", "degraded": True, "note": "官方不提供"})
    decision = run(reviewer.judge("g1", request, mode="standard"))
    assert decision.gate == ""
    assert decision.source == "llm"


def test_missing_profile_policy_three_modes():
    store, _, _, reviewer, _ = make_env()
    degraded = {"kind": "onebot", "degraded": True, "failed": True, "note": "调用失败"}

    manual = run(reviewer.judge("g1", request_payload(profile=degraded), mode="standard"))
    assert manual.auto is False and manual.gate == "profile_missing"

    run(store.update_settings({"join_profile_missing": "decline"}))
    declined = run(reviewer.judge("g1", request_payload(profile=degraded), mode="standard"))
    assert declined.op == "decline" and declined.auto and declined.gate == "profile_missing"
    assert declined.blacklist is False

    run(store.update_settings({"join_profile_missing": "pass"}))
    passed = run(reviewer.judge("g1", request_payload(profile=degraded), mode="standard"))
    assert passed.gate == "" and passed.source == "llm"


def test_account_age_gate_and_actions():
    store, _, _, reviewer, _ = make_env(
        response_text='{"decision":"approve","confidence":0.99,"reason":"正常"}'
    )
    run(store.update_settings({"join_min_account_days": 30}))

    hit = run(
        reviewer.judge("g1", request_payload(profile=_profile(account_age_days=3)), mode="standard")
    )
    assert hit.op == "decline" and hit.auto and hit.gate == "account_age"
    assert hit.blacklist is True  # 跟随 join_decline_blacklist 默认开

    ok = run(
        reviewer.judge(
            "g1", request_payload(profile=_profile(account_age_days=300)), mode="standard"
        )
    )
    assert ok.gate == "" and ok.source == "llm"

    run(store.update_settings({"join_gate_action": "manual"}))
    manual = run(
        reviewer.judge("g1", request_payload(profile=_profile(account_age_days=3)), mode="standard")
    )
    assert manual.auto is False and manual.gate == "account_age"

    run(store.update_settings({"join_gate_action": "pass"}))
    passed = run(
        reviewer.judge("g1", request_payload(profile=_profile(account_age_days=3)), mode="standard")
    )
    assert passed.gate == "" and passed.source == "llm"


def test_qq_level_and_qid_gates():
    store, _, _, reviewer, _ = make_env()
    run(store.update_settings({"join_min_qq_level": 20}))
    low = run(reviewer.judge("g1", request_payload(profile=_profile(qq_level=8)), mode="standard"))
    assert low.gate == "qq_level" and low.op == "decline"

    run(store.update_settings({"join_min_qq_level": 0, "join_require_qid": True}))
    no_qid = run(reviewer.judge("g1", request_payload(profile=_profile(qid="")), mode="standard"))
    assert no_qid.gate == "qid" and no_qid.op == "decline"


def test_missing_fields_do_not_trip_gates():
    store, _, _, reviewer, _ = make_env()
    run(store.update_settings({"join_min_account_days": 30, "join_min_qq_level": 20}))
    profile = _profile(account_age_days=None, qq_level=None)
    decision = run(reviewer.judge("g1", request_payload(profile=profile), mode="standard"))
    assert decision.gate == ""
    assert decision.source == "llm"


def test_profile_injected_into_llm_prompt():
    _, _, _, reviewer, _ = make_env()
    captured = {}

    async def judge_call(system_prompt, user_prompt):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return '{"decision":"approve","confidence":0.9,"reason":"正常"}'

    reviewer.judge_call = judge_call
    decision = run(
        reviewer.judge(
            "g1",
            request_payload(profile=_profile(qq_level=42, account_age_days=100, qid="abc")),
            mode="standard",
        )
    )
    assert "QQ等级=42" in captured["user"]
    assert "账号年龄=100天" in captured["user"]
    assert "【画像完整度】完整" in captured["user"]
    assert "画像缺失" in captured["system"]
    assert decision.gate == ""


def test_profile_persisted_with_gate():
    store, _, audit, reviewer, _ = make_env()
    run(store.update_settings({"join_min_account_days": 30}))
    request = request_payload(
        join_request_id="j9",
        profile=_profile(account_age_days=1, qq_level=3, avatar_url="http://avatar"),
    )
    decision = run(reviewer.judge("g1", request, mode="standard"))
    run(reviewer.submit("g1", request, decision, by="rule"))
    row = audit.joins["j9"]
    assert row["gate"] == "account_age"
    assert row["qq_level"] == 3
    assert row["account_age_days"] == 1
    assert row["profile_source"] == "onebot_stranger"
    assert row["avatar_url"] == "http://avatar"
    assert '"qq_level": 3' in row["profile_json"]


def test_poll_group_enriches_profile_before_judging():
    calls = []

    async def getter(group_id, request):
        calls.append((group_id, request.get("member_openid")))
        return _profile(qq_level=30, account_age_days=900)

    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [request_payload(join_request_id="j1")], "next_cursor": ""},
            ("POST", APPROVE_PATH): {},
        }
    )
    store, _, audit, reviewer, _ = make_env(transport=transport)
    reviewer.profile_getter = getter
    run(store.update_settings({"join_review_mode": "standard"}))
    run(reviewer.poll_group("g1"))
    assert calls == [("g1", "u1")]
    assert audit.joins["j1"]["qq_level"] == 30


def test_poll_group_survives_profile_getter_failure():
    async def broken(group_id, request):
        raise RuntimeError("boom")

    transport = FakeTransport(
        {
            ("GET", LIST_PATH): {"list": [request_payload(join_request_id="j2")], "next_cursor": ""},
            ("POST", APPROVE_PATH): {},
        }
    )
    store, _, _audit, reviewer, _ = make_env(transport=transport)
    reviewer.profile_getter = broken
    run(store.update_settings({"join_review_mode": "standard"}))
    run(reviewer.poll_group("g1"))  # 不抛异常即通过



def test_judge_call_receives_group_id_when_supported():
    _, _, _, reviewer, _ = make_env()
    seen = {}

    async def judge_call(system_prompt, user_prompt, image_urls=None, group_id=''):
        seen['group_id'] = group_id
        seen['images'] = list(image_urls or [])
        return '{"decision":"approve","confidence":0.9,"reason":"正常"}'

    reviewer.judge_call = judge_call
    run(reviewer.judge('g1', request_payload(), mode='standard'))
    assert seen['group_id'] == 'g1'
    assert seen['images'] == []


def test_judge_call_two_arg_signature_is_compatible():
    _, _, _, reviewer, _ = make_env()
    called = {'n': 0}

    async def judge_call(system_prompt, user_prompt):
        called['n'] += 1
        return '{"decision":"approve","confidence":0.9,"reason":"正常"}'

    reviewer.judge_call = judge_call
    decision = run(reviewer.judge('g1', request_payload(), mode='standard'))
    assert decision.op == 'approve'
    assert called['n'] == 1


def test_judge_call_var_keyword_accepts_extra_args():
    _, _, _, reviewer, _ = make_env()
    seen = {}

    async def judge_call(system_prompt, user_prompt, **kwargs):
        seen.update(kwargs)
        return '{"decision":"approve","confidence":0.9,"reason":"正常"}'

    reviewer.judge_call = judge_call
    run(reviewer.judge('g1', request_payload(), mode='standard'))
    assert seen.get('group_id') == 'g1'
