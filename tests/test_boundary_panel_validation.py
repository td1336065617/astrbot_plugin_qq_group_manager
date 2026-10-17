"""面板保存前的边界配置校验 + 保存往返。"""

from __future__ import annotations

import asyncio

from src.models import resolve_boundary_action, resolve_category_policy
from src.store import PluginStore
from src.web_api import validate_boundary_patch
from tests.fakes import FakeKV


def test_valid_patch_passes() -> None:
    data = {
        "category_policy": {
            "其他": {"min": None, "max": 2},
            "政治敏感": {"min": None, "max": 0},
        },
        "boundary_action": {"below": "notify", "above": "silent"},
    }
    assert validate_boundary_patch(data) == ""


def test_min_greater_than_max_rejected() -> None:
    assert "最低等级不能大于最高等级" in validate_boundary_patch(
        {"category_policy": {"其他": {"min": 3, "max": 1}}}
    )


def test_min_with_max_zero_rejected() -> None:
    assert validate_boundary_patch({"category_policy": {"其他": {"min": 1, "max": 0}}}) != ""


def test_unknown_category_rejected() -> None:
    assert "未知类别" in validate_boundary_patch({"category_policy": {"不存在": {"max": 1}}})


def test_bool_and_out_of_range_rejected() -> None:
    assert validate_boundary_patch({"category_policy": {"其他": {"max": True}}}) != ""
    assert validate_boundary_patch({"category_policy": {"其他": {"max": 9}}}) != ""
    assert validate_boundary_patch({"category_policy": {"其他": {"max": -1}}}) != ""


def test_bad_boundary_action_rejected() -> None:
    assert "notify 或 silent" in validate_boundary_patch(
        {"boundary_action": {"above": "ignore"}}
    )


def test_non_dict_payload_rejected() -> None:
    assert validate_boundary_patch({"category_policy": []}) != ""
    assert validate_boundary_patch({"boundary_action": "x"}) != ""


def test_panel_saved_policy_roundtrip() -> None:
    """面板保存后 resolve 立刻生效（不是"改不进去"）。"""

    async def scenario() -> None:
        store = PluginStore(FakeKV())
        await store.load()
        await store.update_settings(
            {
                "category_policy": {
                    "其他": {"min": None, "max": 4},
                    "政治敏感": {"min": None, "max": 0},
                },
                "boundary_action": {"below": "notify", "above": "silent"},
            }
        )
        policy = resolve_category_policy(store.settings())
        assert policy["其他"]["max"] == 4
        assert policy["政治敏感"]["max"] == 0
        assert resolve_boundary_action(store.settings()) == {
            "below": "notify",
            "above": "silent",
        }

    asyncio.run(scenario())
