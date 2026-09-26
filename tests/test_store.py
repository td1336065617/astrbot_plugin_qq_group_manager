"""PluginStore 单元测试（KV 归一化、群配置、成员缓存）。"""

from __future__ import annotations

import asyncio

from src.store import (
    KEY_GROUPS,
    KEY_PROFILE_CACHE,
    KEY_SETTINGS,
    PluginStore,
    normalize_settings,
)
from src.utils import now_ts
from tests.fakes import FakeKV


def run(coro):
    return asyncio.run(coro)


def test_normalize_settings_clamps_and_fills():
    settings = normalize_settings(
        {
            "sample_rate": 5,
            "llm_timeout": 999,
            "mode": "unknown-mode",
            "send_conditions": ["bogus"],
            "mute_steps": "bad",
        }
    )
    assert settings["sample_rate"] == 1.0
    assert settings["llm_timeout"] == 120
    assert settings["mode"] == "lenient"
    assert settings["send_conditions"] == ["rule_hit"]
    assert settings["mute_steps"]["4"] == 3600


def test_first_run_writes_defaults():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    assert kv.data[KEY_SETTINGS]["dry_run"] is True
    assert kv.data[KEY_SETTINGS]["mode"] == "lenient"
    assert store.dry_run() is True


def test_update_settings_persists():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    updated = run(store.update_settings({"dry_run": False, "mode": "standard"}))
    assert updated["dry_run"] is False
    assert kv.data[KEY_SETTINGS]["mode"] == "standard"


def test_group_lifecycle():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.touch_group("g1", name="测试群"))
    config = store.group("g1")
    assert config is not None and config.name == "测试群"
    run(store.update_group("g1", {"moderation_enabled": True, "paused_reason": "x"}))
    assert store.group("g1").moderation_enabled is True
    assert store.group("g1").paused_reason == "x"
    run(store.flush())
    assert "g1" in kv.data[KEY_GROUPS]
    assert run(store.remove_group("g1")) is True
    assert store.group("g1") is None


def test_member_and_role_cache():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.remember_member("g1", "u1", name="张三", role="admin"))
    run(store.remember_member("g1", "u2", name="李四", role="member"))
    assert store.member_name("g1", "u1") == "张三"
    assert store.member_role("g1", "u1") == "admin"
    assert store.is_group_admin("g1", "u1") is True
    assert store.is_group_admin("g1", "u2") is False
    assert store.find_member_by_name("g1", "@张") == [("u1", "张三")]


def test_trusted_and_blacklist():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.update_trusted("g1", ["u1", " ", "u2"]))
    assert store.trusted("g1") == ["u1", "u2"]
    run(store.update_local_blacklist("g1", ["bad1"]))
    assert store.local_blacklist("g1") == ["bad1"]


def test_kv_failure_keeps_dirty_and_does_not_raise():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    kv.fail_keys.add(KEY_SETTINGS)
    run(store.update_settings({"dry_run": False}))
    assert store.dry_run() is False


def test_prompt_and_flood_settings_are_editable():
    """WebUI 策略页会写入提示词与刷屏阈值，必须能被 normalize 保留（回归）。"""
    from src.store import normalize_settings

    settings = normalize_settings(
        {"prompt_system": "自定义", "prompt_user": "模板", "flood_threshold": 3}
    )
    assert settings["prompt_system"] == "自定义"
    assert settings["prompt_user"] == "模板"
    assert settings["flood_threshold"] == 3
    # 越界值被钳制、类型错误回退默认
    assert normalize_settings({"flood_threshold": 999})["flood_threshold"] == 100
    assert normalize_settings({"flood_threshold": "x"})["flood_threshold"] == 8


def test_profile_cache_roundtrip_and_persistence():
    async def scenario():
        kv = FakeKV()
        store = PluginStore(kv)
        await store.load()
        await store.put_profile(
            "p:10001", {"user_id": "10001", "qq_level": 16, "fetched_at": now_ts()}
        )
        await store.flush()
        assert store.get_profile("p:10001")["qq_level"] == 16
        assert kv.data[KEY_PROFILE_CACHE]["p:10001"]["qq_level"] == 16
        reloaded = PluginStore(kv)
        await reloaded.load()
        assert reloaded.get_profile("p:10001")["qq_level"] == 16
        assert reloaded.get_profile("missing") is None

    asyncio.run(scenario())


def test_drop_expired_profiles():
    async def scenario():
        store = PluginStore(FakeKV())
        await store.load()
        await store.put_profile("old", {"fetched_at": 1})
        await store.put_profile("new", {"fetched_at": now_ts()})
        assert await store.drop_expired_profiles(7) == 1
        assert store.get_profile("old") is None
        assert store.get_profile("new") is not None
        assert await store.drop_expired_profiles(0) == 0

    asyncio.run(scenario())


def test_normalize_join_profile_settings():
    assert normalize_settings({"join_profile_missing": "bad"})["join_profile_missing"] == "manual"
    assert normalize_settings({"join_gate_action": "bad"})["join_gate_action"] == "decline"
    assert normalize_settings({"join_avatar_review": "bad"})["join_avatar_review"] == "off"
    assert normalize_settings({"join_profile_enabled": 0})["join_profile_enabled"] is False
    assert normalize_settings({"join_require_qid": 1})["join_require_qid"] is True
    assert normalize_settings({"join_min_account_days": 10**9})["join_min_account_days"] == 3650
    assert normalize_settings({"join_min_qq_level": 9999})["join_min_qq_level"] == 144
    assert normalize_settings({"join_avatar_only_below": 9})["join_avatar_only_below"] == 1.0
    assert normalize_settings({"join_profile_qpm": 0})["join_profile_qpm"] == 1
    assert normalize_settings({"join_profile_concurrency": 99})["join_profile_concurrency"] == 8


def test_prompt_settings_roundtrip_through_store():
    import asyncio

    from src.store import PluginStore
    from tests.fakes import FakeKV

    async def scenario():
        store = PluginStore(FakeKV())
        await store.load()
        updated = await store.update_settings(
            {"prompt_system": "你是审核员", "prompt_user": "内容：{text}", "flood_threshold": 5}
        )
        assert updated["prompt_system"] == "你是审核员"
        assert updated["prompt_user"] == "内容：{text}"
        assert updated["flood_threshold"] == 5

    asyncio.run(scenario())
