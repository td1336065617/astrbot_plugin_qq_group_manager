"""LLMModerator 单元测试：解析、阈值、缓存、熔断、预算、采样。"""

from __future__ import annotations

import asyncio

from src.moderator import (
    USER_TEMPLATE_DEFAULT,
    LLMModerator,
    ModerationRequest,
    extract_json_object,
    parse_verdict,
)

VALID = (
    '{"verdict":"violation","category":"广告引流","severity":4,'
    '"confidence":0.92,"reason":"含引流链接","suggested_action":"mute_and_recall"}'
)


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def make_settings(**overrides):
    settings = {
        "llm_min_confidence": 0.7,
        "cache_ttl": 600,
        "circuit_break_threshold": 3,
        "sample_rate": 1.0,
        "llm_daily_budget": 0,
        "llm_timeout": 5,
    }
    settings.update(overrides)
    return lambda: dict(settings)


def make_moderator(response=VALID, clock=None, **settings):
    calls = {"n": 0}

    async def provider_call(request, system_prompt, user_prompt):
        calls["n"] += 1
        assert "<<<MESSAGE" in user_prompt
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(calls["n"])
        return response

    moderator = LLMModerator(
        provider_call,
        settings_getter=make_settings(**settings),
        clock=(clock or FakeClock()).monotonic,
    )
    return moderator, calls


def test_extract_json_object_handles_noise_and_nesting():
    assert extract_json_object('前言 {"a": {"b": 1}} 后记') == {"a": {"b": 1}}
    assert extract_json_object('{"text": "包含 } 的字符串", "verdict": "allow"}') == {
        "text": "包含 } 的字符串",
        "verdict": "allow",
    }
    assert extract_json_object("没有 JSON") is None
    assert extract_json_object("") is None


def test_parse_verdict_clamps_and_flags_errors():
    verdict = parse_verdict(VALID, latency_ms=12)
    assert verdict.verdict == "violation"
    assert verdict.category == "广告引流"
    assert verdict.severity == 4
    assert verdict.confidence == 0.92
    assert verdict.latency_ms == 12

    weird = parse_verdict(
        '{"verdict":"nonsense","category":"不存在","severity":99,"confidence":5,'
        '"suggested_action":"explode"}'
    )
    assert weird.verdict == "review"
    assert weird.category == "其他"
    assert weird.severity == 5
    assert weird.confidence == 1.0
    assert weird.suggested_action == "none"

    broken = parse_verdict("模型抽风了")
    assert broken.verdict == "review"
    assert broken.parse_error is True


def test_judge_returns_verdict_and_caches():
    clock = FakeClock()
    moderator, calls = make_moderator(clock=clock)
    request = ModerationRequest(group_id="g1", text="加群送资料", sender_role="member")
    first = run(moderator.judge(request))
    assert first.verdict == "violation"
    assert calls["n"] == 1
    second = run(moderator.judge(request))
    assert second.verdict == "violation"
    assert calls["n"] == 1  # 命中缓存
    assert moderator.stats.cache_hits == 1
    clock.now = 1000.0  # 缓存过期后重新调用
    run(moderator.judge(request))
    assert calls["n"] == 2


def test_low_confidence_downgrades_to_review():
    response = (
        '{"verdict":"violation","category":"辱骂攻击","severity":3,'
        '"confidence":0.4,"reason":"疑似","suggested_action":"mute"}'
    )
    moderator, _ = make_moderator(response=response)
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="你个笨蛋")))
    assert verdict.verdict == "review"
    assert "置信度不足" in verdict.reason
    assert verdict.suggested_action == "none"


def test_failures_trigger_circuit_breaker():
    moderator, calls = make_moderator(response=RuntimeError("boom"))
    request = ModerationRequest(group_id="g1", text="第一条")
    verdict = run(moderator.judge(request))
    assert verdict.verdict == "review"
    assert moderator.stats.failures == 1
    run(moderator.judge(ModerationRequest(group_id="g1", text="第二条")))
    run(moderator.judge(ModerationRequest(group_id="g1", text="第三条")))
    assert moderator.circuit_open() is True
    before = calls["n"]
    run(moderator.judge(ModerationRequest(group_id="g1", text="第四条")))
    assert calls["n"] == before  # 熔断期间不再调用模型


def test_budget_and_sampling_skip_calls():
    moderator, calls = make_moderator(llm_daily_budget=1)
    run(moderator.judge(ModerationRequest(group_id="g1", text="一")))
    assert calls["n"] == 1
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="二")))
    assert calls["n"] == 1
    assert "预算" in verdict.reason

    silent, silent_calls = make_moderator(sample_rate=0.0)
    verdict = run(silent.judge(ModerationRequest(group_id="g1", text="三")))
    assert silent_calls["n"] == 0
    assert "采样" in verdict.reason


def test_should_send_conditions():
    moderator, _ = make_moderator(send_conditions=["rule_hit", "has_link"])
    assert moderator.should_send(
        rule_summary="", has_link=True, long_text=False, new_member=False, flood=False, recent=1
    )
    assert not moderator.should_send(
        rule_summary="", has_link=False, long_text=False, new_member=False, flood=False, recent=1
    )
    only_all, _ = make_moderator(send_conditions=["all"])
    assert only_all.should_send(
        rule_summary="", has_link=False, long_text=False, new_member=False, flood=False, recent=0
    )


def test_unavailable_provider_is_safe():
    moderator = LLMModerator(None, settings_getter=make_settings())
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="hi")))
    assert verdict.verdict == "review"
    assert moderator.available() is False


def test_choose_provider_id_prefers_configured():
    from src.moderator import choose_provider_id

    # 未配置 → 用会话默认
    assert choose_provider_id("", "session-provider") == "session-provider"
    # 配置了且存在 → 用配置的
    assert (
        choose_provider_id("mod-provider", "session-provider", ["mod-provider", "x"])
        == "mod-provider"
    )
    # 配置的已不存在 → 回退会话默认
    assert (
        choose_provider_id("gone", "session-provider", ["session-provider"]) == "session-provider"
    )
    # 无法枚举模型（老版本）时信任配置值
    assert choose_provider_id("mod-provider", "", []) == "mod-provider"
    # 两者都为空 → 空串（由调用方走 get_using_provider_async 兜底）
    assert choose_provider_id("", "", []) == ""


def test_llm_provider_id_is_editable_and_persisted():
    import asyncio

    from src.store import PluginStore, normalize_settings
    from tests.fakes import FakeKV

    assert normalize_settings({"llm_provider_id": "abc"})["llm_provider_id"] == "abc"
    assert normalize_settings({})["llm_provider_id"] == ""

    async def scenario():
        kv = FakeKV()
        store = PluginStore(kv)
        await store.load()
        updated = await store.update_settings({"llm_provider_id": "mod-provider"})
        assert updated["llm_provider_id"] == "mod-provider"
        assert kv.data["settings"]["llm_provider_id"] == "mod-provider"

    asyncio.run(scenario())


def test_context_messages_render_into_user_prompt():
    request = ModerationRequest(
        group_id="g1",
        text="我，秦始皇，打钱",
        sender_name="小明",
        rule_summary="模板:拉群引流",
        context_messages=[
            {"sender": "小红", "text": "哈哈哈哈又来了"},
            {"sender": "小刚", "text": "接梗"},
        ],
    )
    prompt = request.render_user_prompt(USER_TEMPLATE_DEFAULT)
    assert "【群聊上下文】" in prompt
    assert "最近 2 条" in prompt
    assert "小红: 哈哈哈哈又来了" in prompt
    assert "小刚: 接梗" in prompt
    assert "<<<MESSAGE" in prompt
    # 语境在正文之前，"可疑点"在正文之后且带免责说明（避免先入为主）
    assert prompt.index("【群聊上下文】") < prompt.index("<<<MESSAGE")
    assert prompt.index("<<<MESSAGE") < prompt.index("【可疑点】")
    assert "不构成违规证据" in prompt


def test_context_placeholder_is_not_double_rendered():
    request = ModerationRequest(
        group_id="g1",
        text="正文",
        rules_brief="群规",
        context_messages=[{"sender": "群友", "text": "占位符 {rules_brief} 不该被替换"}],
    )
    prompt = request.render_user_prompt(USER_TEMPLATE_DEFAULT)
    assert "占位符 {rules_brief} 不该被替换" in prompt
    assert "群规" in prompt


def test_render_context_without_history():
    request = ModerationRequest(group_id="g1", text="正文")
    assert "暂无历史消息" in request.render_context()
    assert "最近 0 条" in request.render_user_prompt(USER_TEMPLATE_DEFAULT)


def test_parse_verdict_reads_analysis_and_evidence():
    verdict = parse_verdict(
        '{"analysis":"复读经典梗，没有人会被引导去转账","evidence":"","verdict":"allow",'
        '"category":"无","severity":1,"confidence":0.9,"reason":"玩梗","suggested_action":"none"}'
    )
    assert verdict.verdict == "allow"
    assert verdict.analysis == "复读经典梗，没有人会被引导去转账"
    assert verdict.evidence == ""


def test_low_confidence_downgrade_keeps_analysis():
    response = (
        '{"analysis":"像广告但缺可触达渠道","evidence":"加群","verdict":"violation",'
        '"category":"广告引流","severity":3,"confidence":0.4,"reason":"疑似",'
        '"suggested_action":"mute"}'
    )
    moderator, _ = make_moderator(response=response)
    verdict = run(moderator.judge(ModerationRequest(group_id="g1", text="加群")))
    assert verdict.verdict == "review"
    assert "置信度不足" in verdict.reason
    assert verdict.analysis == "像广告但缺可触达渠道"
    assert verdict.evidence == "加群"


def test_context_reaches_provider_prompt():
    seen = {}

    async def provider_call(request, system_prompt, user_prompt):
        seen["system"] = system_prompt
        seen["user"] = user_prompt
        return '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,"reason":"正常"'

    moderator = LLMModerator(provider_call, settings_getter=make_settings())
    request = ModerationRequest(
        group_id="g1",
        text="加群领资料",
        context_messages=[{"sender": "小红", "text": "上一条在聊比赛"}],
    )
    run(moderator.judge(request))
    assert "上一条在聊比赛" in seen["user"]
    # 提示词要求先写 analysis 再给结论
    assert "analysis" in seen["system"]
    assert "evidence" in seen["system"]
