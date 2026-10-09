"""指令 @ 目标解析测试（官方通道文本占位符 / mentions / At 组件三层兜底）。

约定：假件优先（tests/fakes.py），不使用 unittest.mock；
场景常量取自生产实测（BUG 报告 §2）：官方通道「@别人」在正文里是 <@openid>。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import At, Reply

from src.commands import match_command
from src.store import PluginStore
from src.utils import (
    extract_mention_ids,
    normalize_command,
    now_ts,
    strip_mention_tokens,
)
from tests.fakes import FakeKV

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
GROUP = "C8D6272C4A0007EE7AF4A8EF4E781AA9"
TARGET = "0EC6A9B416BF7C0B8DCA07C89C954132"
BOT = "C6ED0C4158B81A7FA3A322E248E6DAD1"


def load_main():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


class FakeMention:
    """官方通道 raw_message.mentions 的元素（AstrBot PatchedGroupMessage._User 同型）。"""

    def __init__(self, openid: str, username: str = "", *, is_you: bool = False) -> None:
        self.id = openid
        self.username = username
        self.is_you = is_you


class FakeEvent:
    """只提供目标解析用到的接口。"""

    def __init__(
        self,
        text: str,
        *,
        mentions: list | None = None,
        components: list | None = None,
        self_id: str = BOT,
        group_id: str = GROUP,
        msg_id: str = "",
        raw_data: dict | None = None,
    ) -> None:
        self.message_str = text
        self._components = list(components or [])
        self.message_obj = SimpleNamespace(
            self_id=self_id,
            raw_message=SimpleNamespace(
                mentions=list(mentions or []), id=msg_id, raw_data=dict(raw_data or {})
            ),
            message_id=msg_id,
            group_id=group_id,
        )

    def get_messages(self):
        return list(self._components)

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def get_self_id(self) -> str:
        return self.message_obj.self_id

    def get_platform_id(self) -> str:
        return "qq_official"

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "小号"


def make_plugin(store=None):
    plugin = object.__new__(load_main().QQGroupManager)
    plugin.store = store
    return plugin


def make_store() -> PluginStore:
    return PluginStore(FakeKV())


# ---------------------------------------------------------------- 文本层


def test_strip_mention_tokens_removes_official_and_cq():
    assert strip_mention_tokens("<@ABC> 禁言 <@!DEF> [CQ:at,qq=123] 10分钟") == "禁言 10分钟"
    assert strip_mention_tokens("[At:C6ED0C4158B81A7FA3A322E248E6DAD1] 禁言") == "禁言"


def test_normalize_command_strips_leading_mention():
    text = normalize_command(f"<@{TARGET}> 禁言 10分钟")
    assert text == "禁言 10分钟"
    assert match_command(text) == ("禁言", ["10分钟"])


def test_normalize_command_keeps_plain_at_nickname():
    """纯文本 @昵称 不是占位符，必须原样保留（否则昵称匹配会失效）。"""
    assert normalize_command("禁言   @张三   10分钟") == "禁言 @张三 10分钟"


def test_extract_mention_ids_order_and_dedup():
    assert extract_mention_ids("<@A> 和 <@B> 还有 <@A>") == ["A", "B"]


def test_extract_mention_ids_skips_placeholder():
    assert extract_mention_ids("<@qq_official> <@REAL>") == ["REAL"]
    assert extract_mention_ids("<@all> <@everyone>") == []


# ---------------------------------------------------------------- 目标层


def test_at_targets_from_official_text():
    """官方通道 @别人 只在正文里（无 At 组件）。"""
    event = FakeEvent(f"禁言 <@{TARGET}> 10分钟")
    assert make_plugin()._at_targets(event) == [(TARGET, "")]


def test_at_targets_prefers_mentions_with_username():
    event = FakeEvent("禁言 10分钟", mentions=[FakeMention(TARGET, "张三")])
    assert make_plugin()._at_targets(event) == [(TARGET, "张三")]


def test_at_targets_ignores_bot_itself():
    """@机器人 时 At 组件与 mentions 里都是机器人自己，必须排除。"""
    event = FakeEvent(
        f"禁言 <@{TARGET}>",
        mentions=[FakeMention(BOT, "爱莉希雅", is_you=True)],
        components=[At(qq=BOT, name="爱莉希雅")],
    )
    assert make_plugin()._at_targets(event) == [(TARGET, "")]


def test_at_targets_empty_when_only_bot_mentioned():
    event = FakeEvent("你好", mentions=[FakeMention(BOT, "爱莉希雅", is_you=True)])
    assert make_plugin()._at_targets(event) == []


def test_resolve_target_uses_live_name_over_cache():
    store = make_store()
    asyncio.run(store.remember_member(GROUP, TARGET, name="缓存里的旧名"))
    event = FakeEvent("禁言 10分钟", mentions=[FakeMention(TARGET, "现场新名")])
    assert make_plugin(store)._resolve_target(event, GROUP, ["10分钟"]) == (TARGET, "现场新名")


def test_resolve_target_falls_back_to_cache_name():
    store = make_store()
    asyncio.run(store.remember_member(GROUP, TARGET, name="缓存名"))
    event = FakeEvent(f"禁言 <@{TARGET}> 10分钟")
    assert make_plugin(store)._resolve_target(event, GROUP, ["10分钟"]) == (TARGET, "缓存名")


def test_resolve_target_bare_openid_still_works():
    """实测 A2：裸 openid 是既有有效能力，不得回归。"""
    store = make_store()
    asyncio.run(store.remember_member(GROUP, TARGET, name="缓存名"))
    event = FakeEvent(f"禁言 {TARGET} 10分钟")
    assert make_plugin(store)._resolve_target(event, GROUP, [TARGET, "10分钟"]) == (TARGET, "缓存名")


def test_resolve_target_rejects_wrapped_openid():
    """实测 A1：<@…> 外壳过去被 len>=24 当成 openid 直送平台。"""
    store = make_store()
    event = FakeEvent("禁言 10分钟")  # 事件正文里没有 @ 标记
    assert make_plugin(store)._resolve_target(event, GROUP, [f"<@{TARGET}>", "10分钟"]) == ("", "")


def test_resolve_target_prefers_text_target_over_bot_mention():
    """实测 A5：@机器人 + @目标 时，目标必须是目标，不是机器人。"""
    store = make_store()
    event = FakeEvent(f"禁言 <@{TARGET}>", components=[At(qq=BOT, name="爱莉希雅")])
    assert make_plugin(store)._resolve_target(event, GROUP, []) == (TARGET, "")


# ---------------------------------------------------------------- 撤回留痕（F5）


def test_recall_without_any_target_reports_clearly():
    """拿不到目标时给明确回执，而不是把指令自身 ID 发去平台。"""
    plugin = object.__new__(load_main().QQGroupManager)
    plugin._recent_msgs = {}
    event = FakeEvent("撤回")  # 无引用、无消息 ID
    replies = asyncio.run(plugin._cmd_recall(event, GROUP))
    assert len(replies) == 1 and "无法确定要撤回哪条消息" in replies[0]


def test_recall_prefers_reply_id_when_present():
    plugin = object.__new__(load_main().QQGroupManager)
    plugin._recent_msgs = {}
    event = FakeEvent("撤回", components=[Reply(id="MSG1")])
    assert plugin._resolve_recall_target(event, GROUP, plugin._reply_message_id(event)) == (
        "MSG1",
        "reply",
    )


def test_recall_uses_recent_message_by_quoted_text():
    """官方通道引用载荷没有消息 ID，靠引用正文回溯到真实 message_id。"""
    plugin = object.__new__(load_main().QQGroupManager)
    plugin._recent_msgs = {}
    plugin._remember_recent_message(GROUP, "MSG9", "u1", f"禁言 <@{TARGET}> 1分钟")
    event = FakeEvent("撤回", components=[Reply(id="", chain=[], message_str="禁言   1分钟")])
    assert plugin._resolve_recall_target(event, GROUP, "") == ("MSG9", "recent")


def test_recall_window_matches_platform_limit():
    """回溯窗口必须 ≤ 平台可撤回时限（2 分钟），否则会出现「回溯成功→平台拒绝」。"""
    main = load_main()
    assert main.RECALL_LOOKBACK_SECONDS <= 120


def test_recall_refusal_mentions_platform_limit():
    plugin = object.__new__(load_main().QQGroupManager)
    plugin._recent_msgs = {}
    replies = asyncio.run(plugin._cmd_recall(FakeEvent("撤回"), GROUP))
    assert "2 分钟" in replies[0]


def test_recall_prefers_ordinary_member_over_admin():
    """同一正文命中多条时优先普通成员：群主/管理员的消息平台必然拒撤（40062003）。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    plugin._remember_recent_message(GROUP, "MSG-MEMBER", "u1", "同一个文本", None, "member")
    plugin._remember_recent_message(GROUP, "MSG-ADMIN", "u9", "同一个文本", None, "owner")
    event = FakeEvent("撤回", components=[Reply(id="", chain=[], message_str="同一个文本")])
    assert plugin._resolve_recall_target(event, GROUP, "") == ("MSG-MEMBER", "recent")


def test_sender_meta_falls_back_to_raw_author_role():
    """官方通道 _User 补丁没有 member_role，角色必须从 raw_data.result 里补。"""
    main = load_main()
    event = FakeEvent("你好", msg_id="MSG-1")
    event.message_obj.raw_message.author = SimpleNamespace(
        member_openid="U1", username="小明"
    )
    event.message_obj.raw_message.raw_data = {"author": {"member_role": "owner"}}
    assert main.QQGroupManager._sender_meta(event) == ("U1", "小明", "owner")


def test_recall_refuses_owner_quoted_message_without_api_call():
    """引用元素自带 author.member_role：群主/管理员的消息不必再打一次必拒的接口。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    called: list = []

    class SpyApi:
        async def recall_message(self, group_id, message_id, *, caller="moderation"):
            called.append(message_id)
            return {}

    plugin.audit = None
    plugin.api = SpyApi()
    plugin.logger = logging.getLogger("test-recall-owner")
    event = FakeEvent(
        "撤回",
        components=[Reply(id="", chain=[], message_str="我的消息")],
        raw_data={
            "message_type": 103,
            "author": {"member_role": "member"},
            "msg_elements": [
                {"content": "我的消息", "author": {"member_role": "owner"}},
            ],
        },
    )
    replies = asyncio.run(plugin._cmd_recall(event, GROUP))
    assert "40062003" in replies[0]
    assert called == []


def test_recall_refuses_when_matched_message_is_admin():
    """回溯命中的那条是群主/管理员发的：平台必然拒（40062003），不打接口。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    plugin._remember_recent_message(GROUP, "MSG-ADMIN", "u9", "撤回我", None, "owner")
    called: list = []

    class SpyApi:
        async def recall_message(self, group_id, message_id, *, caller="moderation"):
            called.append(message_id)
            return {}

    plugin.audit = None
    plugin.api = SpyApi()
    plugin.logger = logging.getLogger("test-recall-admin-hit")
    event = FakeEvent("撤回", components=[Reply(id="", chain=[], message_str="撤回我")])
    replies = asyncio.run(plugin._cmd_recall(event, GROUP))
    assert "40062003" in replies[0]
    assert called == []


def test_mute_refuses_admin_target_without_api_call():
    """禁言群主/管理员会被平台拒（40103004）；角色已知时直接说明。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    plugin.store = SimpleNamespace(
        member_role=lambda group_id, openid: "admin",
        member_name=lambda group_id, openid: "群主",
    )
    called: list = []

    class SpyApi:
        async def mute_member(self, group_id, openid, *, seconds=0, caller="moderation"):
            called.append(openid)
            return {}

    plugin.api = SpyApi()
    plugin.audit = None
    plugin.logger = logging.getLogger("test-mute-admin")
    event = FakeEvent(f"禁言 {TARGET} 10分钟")
    replies = asyncio.run(plugin._cmd_mute(event, GROUP, [TARGET, "10分钟"], "道"))
    assert "40103004" in replies[0]
    assert called == []


def test_recall_permission_failure_explains_platform_rule():
    """回溯到的消息若撤不了，要按官方文档说明原因，而不是笼统说没权限。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    plugin._remember_recent_message(GROUP, "MSG9", "u1", "撤回我")

    class ForbiddenApi:
        async def recall_message(self, group_id, message_id, *, caller="moderation"):
            raise main.QQApiError("无操作权限", err_code=40062003, semantic="forbidden")

    plugin.audit = None
    plugin.api = ForbiddenApi()
    plugin.logger = logging.getLogger("test-recall-forbidden")
    event = FakeEvent("撤回", components=[Reply(id="", chain=[], message_str="撤回我")])
    replies = asyncio.run(plugin._cmd_recall(event, GROUP))
    assert "群主/管理员" in replies[0]


def test_recall_refuses_placeholder_only_quote():
    """引用纯图片/表情消息：占位符不是正文，绝不拿它去匹配（会误伤别人）。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    event = FakeEvent(
        "撤回",
        components=[Reply(id="", chain=[], message_str="[表情]")],
        raw_data={"msg_elements": [{"content": "[表情]", "msg_idx": "REFIDX_X"}]},
    )
    assert plugin._resolve_recall_target(event, GROUP, "") == ("", "")


def test_recent_index_skips_placeholder_only_messages():
    """占位符消息不入索引：否则同群任何一条表情消息都会成为撤回目标。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    plugin._remember_recent_message(GROUP, "MSG-IMG", "u1", "[表情]")
    plugin._remember_recent_message(GROUP, "MSG-TXT", "u1", "今天[图片]")
    plugin._remember_recent_message(GROUP, "MSG-EMPTY", "u1", "")
    assert [item["msg_id"] for item in plugin._recent_msgs[GROUP]] == ["MSG-TXT"]


def test_placeholder_text_helper():
    from src.utils import is_placeholder_text

    assert is_placeholder_text("[表情]") is True
    assert is_placeholder_text("[表情][图片]") is True
    assert is_placeholder_text("你好[图片]") is False
    assert is_placeholder_text("") is False


def test_recall_ignores_recent_message_out_of_window():
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    plugin._remember_recent_message(GROUP, "MSG9", "u1", "禁言 1分钟")
    plugin._recent_msgs[GROUP][0]["ts"] = now_ts() - (main.RECALL_LOOKBACK_SECONDS + 1)
    event = FakeEvent("撤回", components=[Reply(id="", chain=[], message_str="禁言 1分钟")])
    assert plugin._resolve_recall_target(event, GROUP, "") == ("", "")


class _SilentApi:
    """只满足 on_group_message 早期调用（_bind_event / _handle_onebot_request）。"""

    available = False
    kind = "official"

    def bind_event(self, event) -> None:
        return None


def test_group_message_populates_recent_index():
    """接线测试：群消息必须真的进入回溯索引（否则撤回永远回溯不到）。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin.store = make_store()
    plugin.api = _SilentApi()
    plugin.audit = None
    plugin.logger = logging.getLogger("qqgm-recent")
    plugin._platform_id = ""
    plugin._recent_msgs = {}

    async def run():
        event = FakeEvent("随便聊聊", msg_id="MSG-1")
        async for _ in main.QQGroupManager.on_group_message(plugin, event):
            pass

    asyncio.run(run())
    assert [item["msg_id"] for item in plugin._recent_msgs.get(GROUP, [])] == ["MSG-1"]


def test_recent_message_index_is_bounded():
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin._recent_msgs = {}
    for index in range(main.RECENT_MSG_PER_GROUP + 10):
        plugin._remember_recent_message(GROUP, f"MSG{index}", "u1", f"内容{index}")
    bucket = plugin._recent_msgs[GROUP]
    assert len(bucket) == main.RECENT_MSG_PER_GROUP
    assert bucket[-1]["msg_id"] == f"MSG{main.RECENT_MSG_PER_GROUP + 9}"


def test_recall_records_message_id_on_failure():
    """撤回失败必须留下「实际发给平台的 message_id 及来源」，用于定论路径。"""
    main = load_main()
    plugin = object.__new__(main.QQGroupManager)
    recorded: list[dict] = []

    class FakeAudit:
        def record_action(self, **payload):
            recorded.append(payload)
            return True

    class FailingApi:
        async def recall_message(self, group_id, message_id, *, caller="moderation"):
            raise main.QQApiError(
                "无操作权限",
                semantic="forbidden",
                hint="机器人没有该操作的权限（多为非群管理员）",
            )

    plugin.audit = FakeAudit()
    plugin.api = FailingApi()
    plugin.logger = logging.getLogger("test-recall")
    event = FakeEvent("撤回", components=[Reply(id="MSG1")])

    replies = asyncio.run(plugin._cmd_recall(event, GROUP))
    assert replies == ["撤回失败：机器人没有该操作的权限（多为非群管理员）"]
    assert recorded and recorded[0]["action"] == "recall_failed"
    assert "message_id=MSG1" in recorded[0]["err_msg"]
    assert recorded[0]["err_msg"].endswith("source=reply")

