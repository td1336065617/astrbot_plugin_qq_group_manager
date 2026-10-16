"""0.15.3 判定阶段（stage）语义测试：9 个来源 + 默认路径 + 非法值留痕 + 降级重建保护。"""

from __future__ import annotations

import asyncio

from src.models import Verdict, default_settings
from src.moderator import LLMModerator, ModerationRequest, parse_verdict


def run(coro):
    return asyncio.run(coro)


def make_settings(**overrides):
    settings = dict(default_settings())
    settings.update({
        "llm_timeout": 5,
        "sample_rate": 1.0,
        "llm_min_confidence": 0.7,
        "cache_ttl": 0,
    })
    settings.update(overrides)
    return lambda: dict(settings)


def make_moderator(response="", **settings):
    async def provider_call(request, system_prompt, user_prompt):
        if isinstance(response, Exception):
            raise response
        return response

    return LLMModerator(provider_call, settings_getter=make_settings(**settings), clock=lambda: 0.0)


def request_of(text) -> ModerationRequest:
    return ModerationRequest(group_id="g1", text=text)


VALID = '{"verdict": "allow", "category": "无", "confidence": 0.9, "reason": "正常交流"}'


def test_review_default_stage_is_judged():
    assert Verdict.review("x").stage == "judged"


def test_parse_failure_marks_parse_error():
    verdict = parse_verdict("这不是 JSON")
    assert verdict.verdict == "review"
    assert verdict.stage == "parse_error"
    assert verdict.parse_error is True


def test_illegal_verdict_marks_illegal_value():
    verdict = parse_verdict('{"verdict": "ban", "category": "其他", "confidence": 0.99}')
    assert verdict.verdict == "review"
    assert verdict.stage == "illegal_value"


def test_legal_verdict_keeps_judged_stage():
    assert parse_verdict(VALID).stage == "judged"


def test_no_provider_stage():
    moderator = LLMModerator(None, settings_getter=make_settings(), clock=lambda: 0.0)
    assert run(moderator.judge(request_of("a"))).stage == "no_provider"


def test_circuit_open_stage():
    moderator = make_moderator(VALID)
    moderator.stats.circuit_open_until = 10.0
    assert run(moderator.judge(request_of("a"))).stage == "circuit_open"


def test_budget_stage():
    moderator = make_moderator(VALID)
    moderator._budget_exhausted = lambda: True  # type: ignore[method-assign]
    assert run(moderator.judge(request_of("a"))).stage == "budget"


def test_sampled_out_stage():
    moderator = make_moderator(VALID, sample_rate=0.0)
    assert run(moderator.judge(request_of("a"))).stage == "sampled_out"


def test_throttled_stage():
    moderator = make_moderator(VALID)

    async def deny(group_id: str = "") -> bool:
        return False

    moderator.acquire = deny  # type: ignore[method-assign]
    assert run(moderator.judge(request_of("a"))).stage == "throttled"


def test_api_error_stage():
    moderator = make_moderator(RuntimeError("boom"))
    assert run(moderator.judge(request_of("a"))).stage == "api_error"


def test_judged_stage_on_success():
    moderator = make_moderator(VALID)
    assert run(moderator.judge(request_of("a"))).stage == "judged"


def test_low_confidence_rebuild_marks_low_confidence():
    moderator = make_moderator('{"verdict": "violation", "category": "广告引流", "confidence": 0.2, "reason": "x"}')
    verdict = run(moderator.judge(request_of("a")))
    assert verdict.verdict == "review"
    assert verdict.stage == "low_confidence"


def test_low_confidence_rebuild_keeps_illegal_value():
    moderator = make_moderator('{"verdict": "ban", "category": "其他", "confidence": 0.1, "reason": "x"}')
    verdict = run(moderator.judge(request_of("a")))
    assert verdict.verdict == "review"
    assert verdict.stage == "illegal_value"

def test_stage_and_reason_are_consistent():
    """stage 与 reason 文案必须一一对应（防止 stage 说 A、文案说 B）。"""
    no_provider = LLMModerator(None, settings_getter=make_settings(), clock=lambda: 0.0)
    verdict = run(no_provider.judge(request_of("a")))
    assert (verdict.stage, verdict.reason) == ("no_provider", "未配置可用的对话模型")

    circuit = make_moderator(VALID)
    circuit.stats.circuit_open_until = 10.0
    verdict = run(circuit.judge(request_of("a")))
    assert (verdict.stage, verdict.reason) == ("circuit_open", "LLM 审核处于熔断期")

    budget = make_moderator(VALID)
    budget._budget_exhausted = lambda: True  # type: ignore[method-assign]
    verdict = run(budget.judge(request_of("a")))
    assert (verdict.stage, verdict.reason) == ("budget", "已达当日 LLM 调用预算")

    sampled = make_moderator(VALID, sample_rate=0.0)
    verdict = run(sampled.judge(request_of("a")))
    assert (verdict.stage, verdict.reason) == ("sampled_out", "本次消息未命中采样")

    throttled = make_moderator(VALID)

    async def deny(group_id: str = "") -> bool:
        return False

    throttled.acquire = deny  # type: ignore[method-assign]
    verdict = run(throttled.judge(request_of("a")))
    assert (verdict.stage, verdict.reason) == ("throttled", "已触发单群 QPM 限流")

    api = make_moderator(RuntimeError("boom"))
    verdict = run(api.judge(request_of("a")))
    assert verdict.stage == "api_error" and verdict.reason.startswith("模型调用失败：")

    parse_fail = make_moderator("这不是 JSON 也不是键值行")
    verdict = run(parse_fail.judge(request_of("a")))
    assert verdict.stage == "parse_error" and verdict.reason == "模型未按 JSON 输出（已降级）"
