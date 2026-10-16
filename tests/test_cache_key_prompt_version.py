"""T5：缓存键包含 prompt_version（改判词后不得命中旧结论）。"""

from __future__ import annotations

import asyncio

import src.moderator as moderator_module
from src.models import default_settings
from src.moderator import LLMModerator, ModerationRequest


def run(coro):
    return asyncio.run(coro)


RESPONSE = '{"verdict": "allow", "category": "无", "confidence": 0.9, "reason": "正常"}'


def make_moderator():
    calls = {"n": 0}

    async def provider_call(request, system_prompt, user_prompt):
        calls["n"] += 1
        return RESPONSE

    settings = dict(default_settings())
    settings.update({"llm_timeout": 5, "sample_rate": 1.0, "cache_ttl": 600})
    moderator = LLMModerator(provider_call, settings_getter=lambda: dict(settings), clock=lambda: 0.0)
    return moderator, calls


def test_same_prompt_version_hits_cache():
    moderator, calls = make_moderator()
    request = ModerationRequest(group_id="g1", text="同一条消息")
    run(moderator.judge(request))
    run(moderator.judge(ModerationRequest(group_id="g1", text="同一条消息")))
    assert calls["n"] == 1
    assert moderator.stats.cache_hits == 1


def test_prompt_version_change_busts_cache(monkeypatch):
    moderator, calls = make_moderator()
    run(moderator.judge(ModerationRequest(group_id="g1", text="同一条消息")))
    monkeypatch.setattr(moderator_module, "PROMPT_VERSION", "2026-10-17-b2")
    run(moderator.judge(ModerationRequest(group_id="g1", text="同一条消息")))
    assert calls["n"] == 2


def test_custom_template_busts_cache():
    moderator, calls = make_moderator()
    run(moderator.judge(ModerationRequest(group_id="g1", text="同一条消息")))
    run(moderator.judge(ModerationRequest(group_id="g1", text="同一条消息"), templates={"system": "自定义"}))
    assert calls["n"] == 2
