"""ApplicantProfileService 单元测试：缓存 / 降级 / 熔断 / 限频。"""

from __future__ import annotations

import asyncio

from src.platforms.onebot import OneBotChannel
from src.profiles import ApplicantProfileService
from src.store import PluginStore
from tests.fakes import FakeKV
from tests.test_platforms import FakeOneBotClient

GOOD = {"get_stranger_info": {"nickname": "小号", "qqLevel": 12, "reg_time": 1600000000}}


def run(coro):
    return asyncio.run(coro)


def make_service(responses=None, error=None, settings=None):
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.ensure_group("g1", name="测试群", platform_id="onebot-1"))
    if settings:
        run(store.update_settings(settings))
    bot = FakeOneBotClient(responses=responses or {}, error=error)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    service = ApplicantProfileService(
        store=store,
        channel_for=lambda pid: OneBotChannel(bot, pid),
        sleep=fake_sleep,
    )
    return service, store, bot, sleep_calls


def test_profile_cache_hit_skips_channel():
    service, _store, bot, _sleep = make_service(responses=GOOD)
    first = run(service.get("g1", {"user_id": "10001"}))
    assert first["degraded"] is False
    assert first["source"] == "onebot_stranger"
    second = run(service.get("g1", {"user_id": "10001"}))
    assert second["source"] == "cache"
    assert len(bot.calls) == 1


def test_degraded_profile_is_not_cached():
    service, _store, bot, _sleep = make_service(
        responses={"get_stranger_info": {"nickname": "只有昵称"}}
    )
    first = run(service.get("g1", {"user_id": "10001"}))
    assert first["degraded"] is True
    run(service.get("g1", {"user_id": "10001"}))
    assert len(bot.calls) == 2


def test_channel_failure_degrades_without_raising():
    service, _store, _bot, _sleep = make_service(error=RuntimeError("boom"))
    profile = run(service.get("g1", {"user_id": "10001"}))
    assert profile["degraded"] is True
    assert profile["failed"] is True
    assert "失败" in profile["note"]


def test_breaker_opens_after_repeated_failures():
    service, _store, bot, _sleep = make_service(error=RuntimeError("boom"))
    for index in range(5):
        run(service.get("g1", {"user_id": f"1000{index}"}))
    assert service.stats()["breaker_open"] is True
    calls_before = len(bot.calls)
    profile = run(service.get("g1", {"user_id": "10009"}))
    assert "熔断" in profile["note"]
    assert len(bot.calls) == calls_before


def test_disabled_by_setting_skips_channel():
    service, _store, bot, _sleep = make_service(
        responses=GOOD, settings={"join_profile_enabled": False}
    )
    profile = run(service.get("g1", {"user_id": "10001"}))
    assert profile["degraded"] is True
    assert "已关闭" in profile["note"]
    assert bot.calls == []


def test_rate_limit_waits_instead_of_dropping():
    service, _store, bot, sleep_calls = make_service(
        responses=GOOD, settings={"join_profile_qpm": 1}
    )
    run(service.get("g1", {"user_id": "10001"}))
    run(service.get("g1", {"user_id": "10002"}))
    assert sleep_calls and sleep_calls[0] > 0
    assert len(bot.calls) == 2


def test_cache_ttl_zero_disables_cache():
    service, _store, bot, _sleep = make_service(
        responses=GOOD, settings={"join_profile_cache_days": 0}
    )
    run(service.get("g1", {"user_id": "10001"}))
    run(service.get("g1", {"user_id": "10001"}))
    assert len(bot.calls) == 2


def test_group_without_platform_id_still_works():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.ensure_group("g9", name="无平台群"))
    bot = FakeOneBotClient(responses=GOOD)

    async def fake_sleep(_seconds):
        return None

    service = ApplicantProfileService(
        store=store, channel_for=lambda pid: OneBotChannel(bot, pid), sleep=fake_sleep
    )
    profile = run(service.get("g9", {"user_id": "10001"}))
    assert profile["nickname"] == "小号"
