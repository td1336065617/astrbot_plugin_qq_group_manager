"""QQ 双通道平台层测试：通道判定、OneBot 映射、降级与路由。"""
from __future__ import annotations

import asyncio

import pytest

from src.api_client import QQApiError
from src.models import (
    CAP_BLACKLIST,
    CAP_IS_ADMIN,
    CAP_MEMBER_LIST,
    CAPABILITIES,
)
from src.platforms.base import channel_kind
from src.platforms.null import NullChannel
from src.platforms.official import OfficialChannel
from src.platforms.onebot import OneBotChannel
from src.platforms.router import ChannelRouter


class FakeOneBotClient:
    def __init__(self, responses=None, error=None):
        self.calls = []
        self.responses = responses or {}
        self.error = error

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error is not None:
            raise self.error
        return self.responses.get(action, {})


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _MsgObj:
    def __init__(self, raw=None):
        self.raw_message = raw
        self.message = []


class FakeEvent:
    def __init__(self, name, platform_id="p1", *, bot=None, raw=None, self_id=""):
        self._name = name
        self._pid = platform_id
        self.bot = bot
        self.message_obj = _MsgObj(raw)
        self._self_id = self_id

    def get_platform_name(self):
        return self._name

    def get_platform_id(self):
        return self._pid

    def get_self_id(self):
        return self._self_id


class _Meta:
    def __init__(self, name, pid):
        self.name = name
        self.id = pid


class _Inst:
    def __init__(self, name, pid, bot=None):
        self._meta = _Meta(name, pid)
        self.bot = bot

    def meta(self):
        return self._meta


class _Ctx:
    def __init__(self, insts):
        self._insts = insts

    def get_platform_inst(self, pid):
        for inst in self._insts:
            if inst.meta().id == pid:
                return inst
        return None


def test_channel_kind():
    assert channel_kind("qq_official") == "official"
    assert channel_kind("qq_official_webhook") == "official"
    assert channel_kind("aiocqhttp") == "onebot"
    assert channel_kind("telegram") == ""


def test_official_channel_delegates():
    class FakeApi:
        available = True

        def dry_run(self):
            return True

        async def recall_message(self, group_id, message_id, *, caller="moderation"):
            return {"ok": group_id, "id": message_id}

    ch = OfficialChannel(FakeApi(), "p1")
    assert ch.kind == "official"
    assert ch.available is True
    assert ch.dry_run() is True
    assert asyncio.run(ch.recall_message("g1", "m1"))["id"] == "m1"


def test_null_channel_degrades():
    ch = NullChannel("p1", dry_run_getter=lambda: False)
    assert ch.available is False
    results = asyncio.run(ch.probe("g1"))
    assert set(results) == set(CAPABILITIES)
    assert all(not item.ok for item in results.values())
    with pytest.raises(QQApiError):
        asyncio.run(ch.recall_message("g1", "m1"))


def test_onebot_recall_and_mute():
    bot = FakeOneBotClient()
    ch = OneBotChannel(bot, "onebot-1", self_id="12345")
    asyncio.run(ch.recall_message("999", "555"))
    asyncio.run(ch.mute_member("999", "888", seconds=60))
    actions = [c[0] for c in bot.calls]
    assert actions == ["delete_msg", "set_group_ban"]
    assert bot.calls[0][1]["message_id"] == 555
    assert bot.calls[1][1]["user_id"] == 888
    assert bot.calls[1][1]["duration"] == 60


def test_onebot_blacklist_is_degraded_kick():
    bot = FakeOneBotClient()
    ch = OneBotChannel(bot, "onebot-1")
    result = asyncio.run(ch.update_blacklist("999", op="add", member_openids=["888"]))
    assert result["degraded"] is True
    assert bot.calls[0][0] == "set_group_kick"
    assert bot.calls[0][1]["reject_add_request"] is True


def test_onebot_dry_run_blocks_write():
    bot = FakeOneBotClient()
    ch = OneBotChannel(bot, "onebot-1", dry_run_getter=lambda: True)
    result = asyncio.run(ch.mute_member("999", "888", seconds=60))
    assert result["_dry_run"] is True
    assert bot.calls == []


def test_onebot_probe_capabilities():
    bot = FakeOneBotClient(
        responses={
            "get_group_info": {"group_name": "g"},
            "get_group_member_info": {"role": "admin"},
            "get_group_member_list": [{"user_id": 1}],
        }
    )
    ch = OneBotChannel(bot, "onebot-1", self_id="12345")
    results = asyncio.run(ch.probe("999"))
    assert results[CAP_IS_ADMIN].ok is True
    assert results[CAP_MEMBER_LIST].ok is True
    assert results[CAP_BLACKLIST].ok is False
    assert "降级" in results[CAP_BLACKLIST].note


def test_onebot_join_request_event_flow():
    bot = FakeOneBotClient()
    ch = OneBotChannel(bot, "onebot-1")
    raw = {
        "post_type": "request",
        "request_type": "group",
        "sub_type": "add",
        "flag": "flag-1",
        "group_id": "999",
        "user_id": "888",
        "comment": "求进群",
    }
    request = ch.feed_request(FakeEvent("aiocqhttp", raw=raw))
    assert request is not None and request["join_request_id"] == "flag-1"
    listed = asyncio.run(ch.join_request_list("999"))
    assert len(listed["list"]) == 1
    # 取出后缓存清空
    assert asyncio.run(ch.join_request_list("999"))["list"] == []
    asyncio.run(
        ch.approve_join_request(
            "999", "888", op="approve", join_request_id="flag-1"
        )
    )
    assert bot.calls[-1][0] == "set_group_add_request"
    assert bot.calls[-1][1]["flag"] == "flag-1"
    assert bot.calls[-1][1]["approve"] is True


def test_router_bind_official_and_onebot():
    base = _Obj(available=True, dry_run=lambda: False, transport=None, audit=None)
    bot = FakeOneBotClient()
    ctx = _Ctx([_Inst("aiocqhttp", "onebot-1", bot=bot), _Inst("qq_official", "爱莉希雅")])
    router = ChannelRouter(base, dry_run_getter=lambda: False, context=ctx)

    router.bind_event(FakeEvent("qq_official", "爱莉希雅"))
    assert router.kind == "official"
    assert router.available is True

    router.bind_event(FakeEvent("aiocqhttp", "onebot-1", bot=bot))
    assert router.kind == "onebot"

    router.bind_event(FakeEvent("telegram", "tg-1"))
    assert router.kind == "null"

    assert router.kind_for("aiocqhttp-unknown") == ""
    assert router.kind_for("爱莉希雅") == "official"


def test_router_audit_setattr_forwards_to_base():
    base = _Obj(available=True, dry_run=lambda: False, transport=None, audit=None)
    router = ChannelRouter(base, dry_run_getter=lambda: False)
    sentinel = object()
    router.audit = sentinel
    assert base.audit is sentinel
    assert router.audit is sentinel
