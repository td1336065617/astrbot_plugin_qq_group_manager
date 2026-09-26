"""QQ 双通道平台层测试：通道判定、OneBot 映射、降级与路由。"""
from __future__ import annotations

import asyncio

import pytest

from src.api_client import QQApiError, QQGroupAPI
from src.models import (
    CAP_BLACKLIST,
    CAP_GROUP_INFO,
    CAP_IS_ADMIN,
    CAP_MEMBER_LIST,
    CAPABILITIES,
)
from src.platforms.base import channel_kind
from src.platforms.null import NullChannel
from src.platforms.official import OfficialChannel
from src.platforms.onebot import OneBotChannel
from src.platforms.router import ChannelRouter
from tests.fakes import FakeTransport


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


def test_onebot_connected_property():
    bot = FakeOneBotClient()
    ch = OneBotChannel(bot, "onebot-1")
    # 非 aiocqhttp 实现无法判断时保守视为可用
    assert ch.connected is True
    bot._wsr_api_clients = {}
    assert ch.connected is False
    assert ch.available is False
    bot._wsr_api_clients = {"1482759239": object()}
    assert ch.connected is True


def test_onebot_probe_short_circuits_when_disconnected():
    bot = FakeOneBotClient(responses={"get_group_info": {"group_name": "g"}})
    bot._wsr_api_clients = {}
    ch = OneBotChannel(bot, "onebot-1", self_id="12345")
    results = asyncio.run(ch.probe("999"))
    assert bot.calls == []
    assert all(not item.ok for item in results.values())
    assert "未连接" in results[CAP_GROUP_INFO].note


def test_onebot_error_message_uses_exception_type():
    class _Boom(Exception):
        pass

    bot = FakeOneBotClient(error=_Boom())
    ch = OneBotChannel(bot, "onebot-1")
    with pytest.raises(QQApiError) as exc:
        asyncio.run(ch.recall_message("1", "2"))
    assert "_Boom" in str(exc.value)


def test_official_channel_profile_is_degraded_without_requests():
    transport = FakeTransport()
    ch = OfficialChannel(QQGroupAPI(transport), "p1")
    profile = asyncio.run(
        ch.get_applicant_profile({"member_openid": "o1", "username": "张三"})
    )
    assert profile["nickname"] == "张三"
    assert profile["degraded"] is True
    assert profile["source"] == "official_request"
    assert profile["avatar_url"] == ""
    assert transport.calls == []


def test_null_channel_profile_does_not_raise():
    ch = NullChannel("p1")
    profile = asyncio.run(ch.get_applicant_profile({"member_openid": "o1"}))
    assert profile["degraded"] is True
    assert profile["kind"] == "null"


def test_onebot_profile_mapping_and_avatar():
    bot = FakeOneBotClient(
        responses={
            "get_stranger_info": {
                "nickname": "小号",
                "qqLevel": 12,
                "reg_time": 1600000000,
                "qid": "q-1",
                "sex": "male",
                "age": 20,
            }
        }
    )
    ch = OneBotChannel(bot, "onebot-1")
    profile = asyncio.run(ch.get_applicant_profile({"user_id": "10001"}))
    assert profile["nickname"] == "小号"
    assert profile["qq_level"] == 12
    assert profile["qid"] == "q-1"
    assert profile["account_age_days"] > 0
    assert profile["avatar_url"] == "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"
    assert profile["degraded"] is False
    assert bot.calls[0][0] == "get_stranger_info"


def test_onebot_profile_degrades_when_extensions_missing():
    bot = FakeOneBotClient(responses={"get_stranger_info": {"nickname": "标准协议端"}})
    ch = OneBotChannel(bot, "onebot-1")
    profile = asyncio.run(ch.get_applicant_profile({"user_id": "10001"}))
    assert profile["qq_level"] is None
    assert profile["account_age_days"] is None
    assert profile["degraded"] is True


def test_onebot_profile_qq_level_shapes():
    cases = [(16, 16), ("16", 16), ({"level": 16}, 16), (None, None), ("x", None)]
    for raw, expected in cases:
        bot = FakeOneBotClient(
            responses={"get_stranger_info": {"nickname": "n", "qqLevel": raw}}
        )
        ch = OneBotChannel(bot, "onebot-1")
        profile = asyncio.run(ch.get_applicant_profile({"user_id": "10001"}))
        assert profile["qq_level"] == expected, raw


def test_onebot_profile_failure_degrades():
    bot = FakeOneBotClient(error=RuntimeError("boom"))
    ch = OneBotChannel(bot, "onebot-1")
    profile = asyncio.run(ch.get_applicant_profile({"user_id": "10001"}))
    assert profile["degraded"] is True
    assert "get_stranger_info 失败" in profile["note"]


def test_onebot_profile_without_qq_number_skips_call():
    bot = FakeOneBotClient()
    ch = OneBotChannel(bot, "onebot-1")
    profile = asyncio.run(ch.get_applicant_profile({"user_id": "not-a-number"}))
    assert profile["degraded"] is True
    assert bot.calls == []

class _AiocqhttpLikeBot(FakeOneBotClient):
    """模拟 aiocqhttp 的 Api.__getattr__：未知属性一律返回 partial(call_action, 名字)。

    这正是生产事故（BUG-040）的现场：getattr(bot, "self_id") 拿到的是可调用对象。
    """

    def __getattr__(self, item):
        import functools

        return functools.partial(self.call_action, item)


def test_onebot_self_id_rejects_aiocqhttp_magic_attribute():
    bot = _AiocqhttpLikeBot(responses={"get_login_info": {"user_id": 1482759239}})
    ch = OneBotChannel(bot, "onebot-1")
    # 关键：不能把 partial(call_action, "self_id") 当 QQ 号
    assert ch._self_id() == ""
    assert asyncio.run(ch._ensure_self_id()) == "1482759239"
    assert ch._self_id() == "1482759239"  # 已缓存
    assert bot.calls[-1][0] == "get_login_info"


def test_onebot_self_id_prefers_event_cache():
    bot = _AiocqhttpLikeBot(responses={"get_login_info": {"user_id": 1}})
    ch = OneBotChannel(bot, "onebot-1", self_id="12345")
    assert ch._self_id() == "12345"
    assert asyncio.run(ch._ensure_self_id()) == "12345"
    assert bot.calls == []  # 有缓存就不发 API


def test_onebot_probe_skips_dirty_self_id():
    bot = _AiocqhttpLikeBot(responses={})  # get_login_info 返回 {}
    ch = OneBotChannel(bot, "onebot-1")
    asyncio.run(ch.probe("g1"))
    # 取不到 self_id 时不得发 get_group_member_info（避免把脏值传给协议端）
    assert all(action != "get_group_member_info" for action, _ in bot.calls)
