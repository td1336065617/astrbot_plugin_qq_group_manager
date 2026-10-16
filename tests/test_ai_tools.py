"""QQ群管 AI 工具行为测试：门禁 / 权限 / 节流 / 写操作参数透传（实例桩，不启动 AstrBot）。"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

from src.models import default_settings

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _import_main():
    """按 qqgm 既有做法：把插件**父目录**加入 sys.path 后按包导入（main.py 有相对导入）。"""
    parent = str(PLUGIN_ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


qqgm_main = _import_main()


class FakeStore:
    def __init__(self, settings: dict, group_admin: bool = False) -> None:
        self._settings = dict(settings)
        self._group_admin = group_admin

    def settings(self) -> dict:
        return dict(self._settings)

    def find_member_by_name(self, group_id: str, keyword: str) -> list:
        # 与真实 store 同契约：按昵称关键字查在册成员
        needle = (keyword or '').strip().lstrip('@')
        hits = [(k, v) for k, v in getattr(self, "member_cache_pairs", {}).items() if needle and needle in str(v)]
        return hits

    def member_name(self, group_id: str, openid: str) -> str:
        return str(getattr(self, "member_cache_pairs", {}).get(openid, ""))

    def member_names(self, group_id: str, limit: int = 10) -> list:
        # 与真实 store 同契约：找不到人时工具会回列在册成员
        return list(getattr(self, "member_cache_names", []))[:limit]

    def is_group_admin(self, group_id: str, openid: str) -> bool:
        return self._group_admin

    def group_or_default(self, group_id: str):
        return types.SimpleNamespace(name="测试群", mode="")


class FakeActions:
    def __init__(self) -> None:
        self.calls: list = []
        self.result: dict = {"ok": True, "action": "mute", "dry_run": False, "message": "已执行", "detail": {}}

    async def handle_manual(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class FakeEvent:
    def __init__(self, group_id: str = "g1", is_admin: bool = False) -> None:
        self._group_id = group_id
        self._is_admin = is_admin
        self.unified_msg_origin = "aiocqhttp:GroupMessage:" + group_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return "u1"

    def get_messages(self) -> list:
        return []

    # 真实事件都有这两个属性（_at_targets 会读）
    message_str = ""
    message_obj = None

    def is_admin(self) -> bool:
        return self._is_admin


def payload(raw: str) -> dict:
    return json.loads(raw)


def run(coro):
    return asyncio.run(coro)


def make_bot(settings: dict | None = None, *, admin: bool = False, group_admin: bool = False):
    values = default_settings()
    values.update(settings or {})
    bot = qqgm_main.QQGroupManager.__new__(qqgm_main.QQGroupManager)
    bot.store = FakeStore(values, group_admin=group_admin)
    bot.actions = FakeActions()
    bot.audit = None
    bot.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    bot._ai_throttle = None
    return bot


def test_group_info_disabled_when_tools_off() -> None:
    bot = make_bot({"qqgm_ai_tools_enabled": False})
    data = payload(run(bot.qqgm_group_info(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "disabled"


def test_group_info_requires_group() -> None:
    bot = make_bot()
    data = payload(run(bot.qqgm_group_info(FakeEvent(group_id=""))))
    assert data["ok"] is False and data["error"]["code"] == "platform_unsupported"


def test_read_tools_disabled_separately() -> None:
    bot = make_bot({"qqgm_ai_tools_read_enabled": False})
    data = payload(run(bot.qqgm_group_info(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "disabled"


def test_stats_requires_admin() -> None:
    bot = make_bot()
    data = payload(run(bot.qqgm_stats(FakeEvent(), days=1)))
    assert data["ok"] is False and data["error"]["code"] == "permission_denied"


def test_stats_rejects_bad_days() -> None:
    bot = make_bot(group_admin=True)
    data = payload(run(bot.qqgm_stats(FakeEvent(), days="3")))
    assert data["ok"] is False and data["error"]["code"] == "invalid_argument"


def test_recent_events_requires_admin_and_valid_limit() -> None:
    bot = make_bot()
    denied = payload(run(bot.qqgm_recent_events(FakeEvent(), limit=5)))
    assert denied["error"]["code"] == "permission_denied"
    bot_admin = make_bot(group_admin=True)
    bad = payload(run(bot_admin.qqgm_recent_events(FakeEvent(), limit="abc")))
    assert bad["error"]["code"] == "invalid_argument"
    no_audit = payload(run(bot_admin.qqgm_recent_events(FakeEvent(), limit=3)))
    assert no_audit["error"]["code"] == "capability_unavailable"


def test_moderation_status_is_public(monkeypatch) -> None:
    """QG-02：该工具对所有人开放（人类「审核状态」指令仍是群管指令，这里按需求放行）。"""
    bot = make_bot()

    async def fake_status(group_id, sender, admin, group_admin):
        return "审核：已开启（模式=standard）"

    monkeypatch.setattr(bot, "_cmd_status", fake_status)
    ok = payload(run(bot.qqgm_moderation_status(FakeEvent())))
    assert ok["ok"] is True
    assert "已开启" in ok["data"]["text"]
    admin_bot = make_bot(group_admin=True)

    async def fake_status(group_id, sender, admin, group_admin):
        return "审核：已开启（模式=standard）"

    monkeypatch.setattr(admin_bot, "_cmd_status", fake_status)
    ok = payload(run(admin_bot.qqgm_moderation_status(FakeEvent())))
    assert ok["ok"] is True and "已开启" in ok["data"]["text"]


def test_mute_requires_write_switch() -> None:
    bot = make_bot(group_admin=True)
    bot._resolve_target = lambda event, group_id, args: ("u_target", "小明")
    data = payload(run(bot.qqgm_mute_user(FakeEvent(), target="小明", minutes=10)))
    assert data["ok"] is False and data["error"]["code"] == "disabled"
    assert bot.actions.calls == [], "写开关关闭时不得调用执行层"


def test_mute_passes_seconds_and_source(monkeypatch) -> None:
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    monkeypatch.setattr(bot, "_resolve_target", lambda event, group_id, args: ("u_target", "小明"))
    data = payload(run(bot.qqgm_mute_user(FakeEvent(), target="小明", minutes=10, reason="刷屏")))
    assert data["ok"] is True and data["data"]["minutes"] == 10
    call = bot.actions.calls[0]
    assert call["action"] == "mute"
    assert call["seconds"] == 600
    assert call["sender_openid"] == "u_target"
    assert call["source"] == "llm_tool"
    assert call["settings"]["mode"] != "standard", "默认应尊重群模式（lenient/log_only）"


def test_mute_respects_lenient_toggle(monkeypatch) -> None:
    bot = make_bot(
        {"qqgm_ai_tools_write_enabled": True, "qqgm_ai_respect_lenient": False},
        group_admin=True,
    )
    monkeypatch.setattr(bot, "_resolve_target", lambda event, group_id, args: ("u_target", "小明"))
    run(bot.qqgm_mute_user(FakeEvent(), target="小明", minutes=1))
    assert bot.actions.calls[0]["settings"]["mode"] == "standard"


def test_mute_invalid_minutes(monkeypatch) -> None:
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    monkeypatch.setattr(bot, "_resolve_target", lambda event, group_id, args: ("u_target", "小明"))
    print(payload(run(bot.qqgm_mute_user(FakeEvent(), target="小明", minutes=0)))["error"]["code"])
    assert payload(run(bot.qqgm_mute_user(FakeEvent(), target="小明", minutes=0)))["error"]["code"] == "invalid_argument"
    assert payload(run(bot.qqgm_mute_user(FakeEvent(), target="小明", minutes=99999)))["error"]["code"] == "invalid_argument"


def test_mute_target_not_found(monkeypatch) -> None:
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    monkeypatch.setattr(bot, "_resolve_target", lambda event, group_id, args: ("", ""))
    data = payload(run(bot.qqgm_mute_user(FakeEvent(), target="查无此人", minutes=5)))
    assert data["ok"] is False and data["error"]["code"] == "not_found"


def test_unmute_passes_action(monkeypatch) -> None:
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    monkeypatch.setattr(bot, "_resolve_target", lambda event, group_id, args: ("u_target", "小明"))
    data = payload(run(bot.qqgm_unmute_user(FakeEvent(), target="小明")))
    assert data["ok"] is True
    assert bot.actions.calls[0]["action"] == "unmute"
    assert "seconds_override" not in bot.actions.calls[0]


def test_throttle_blocks_after_limit(monkeypatch) -> None:
    bot = make_bot(
        {"qqgm_ai_tools_write_enabled": True, "qqgm_ai_throttle_limit": 2, "qqgm_ai_throttle_window": 60},
        group_admin=True,
    )
    monkeypatch.setattr(bot, "_resolve_target", lambda event, group_id, args: ("u_target", "小明"))
    event = FakeEvent()
    first = payload(run(bot.qqgm_unmute_user(event, target="小明")))
    second = payload(run(bot.qqgm_unmute_user(event, target="小明")))
    third = payload(run(bot.qqgm_unmute_user(event, target="小明")))
    assert first["ok"] is True and second["ok"] is True
    assert third["ok"] is False and third["error"]["code"] == "throttled"


def test_moderation_status_passes_global_admin_flag(monkeypatch) -> None:
    """F1：AI 工具也要把 AstrBot 全局管理员传进 _cmd_status。

    否则同一个全局管理员问"我是否豁免审核"，AI 答"否"、人类指令答"是"。
    """
    bot = make_bot()
    seen: dict = {}

    async def fake_status(group_id, sender, admin, group_admin):
        seen["admin"] = admin
        return "我是否豁免审核：是" if admin else "我是否豁免审核：否"

    monkeypatch.setattr(bot, "_cmd_status", fake_status)
    data = payload(run(bot.qqgm_moderation_status(FakeEvent(is_admin=True))))
    assert data["ok"] is True, data
    assert seen["admin"] is True, seen
    assert "是" in data["data"]["text"]


def test_unmute_not_found_has_recovery_hint(monkeypatch) -> None:
    """F8：unmute 的 not_found 必须和 mute 一样给出自救提示。"""
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)

    def no_target(event, group_id, args):
        return "", ""

    monkeypatch.setattr(bot, "_resolve_target", no_target)
    data = payload(run(bot.qqgm_unmute_user(FakeEvent(), target="查无此人")))
    assert data["ok"] is False and data["error"]["code"] == "not_found"
    assert data["error"].get("hint"), data


def test_mute_accepts_raw_openid_without_member_cache(monkeypatch) -> None:
    """生产实案：模型直接把成员 openid 当 target 传进来时必须能执行。

    这条路径**不依赖**本插件的成员缓存（qq_official 的 openid 是群内唯一标识），
    因此不允许出现"这人不在本群成员列表里"这种拒绝。
    """
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    openid = "DE50381AB242B0C2ACD020DA10DD72AD"
    data = payload(run(bot.qqgm_mute_user(FakeEvent(), target=openid, minutes=10)))
    assert data["ok"] is True, data
    call = bot.actions.calls[0]
    assert call["sender_openid"] == openid, call
    assert call["seconds"] == 600, call


def test_mute_not_found_lists_cached_members(monkeypatch) -> None:
    """找不到人时要把在册成员回列出来，模型才能自己纠正（而不是去问别的插件）。"""
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    monkeypatch.setattr(bot.store, "member_names", lambda group_id, limit=10: ["紫藤沙喵", "道"])
    data = payload(run(bot.qqgm_mute_user(FakeEvent(), target="查无此人", minutes=10)))
    assert data["ok"] is False and data["error"]["code"] == "not_found"
    hint = str(data["error"].get("hint") or "")
    assert "紫藤沙喵" in hint and "openid" in hint, data

def test_mute_dedupe_key_contains_target() -> None:
    """幂等键必须含目标：否则同群第二个不同目标会被误判成重复推送。"""
    bot = make_bot({"qqgm_ai_tools_write_enabled": True}, group_admin=True)
    first = "DE50381AB242B0C2ACD020DA10DD72AD"
    second = "C6ED0C4158B81A7FA3A322E248E6DAD1"
    payload(run(bot.qqgm_mute_user(FakeEvent(), target=first, minutes=5)))
    payload(run(bot.qqgm_mute_user(FakeEvent(), target=second, minutes=5)))
    keys = [call["dedupe_key"] for call in bot.actions.calls]
    assert keys[0].endswith(first) and keys[1].endswith(second), keys
    assert keys[0] != keys[1], keys
