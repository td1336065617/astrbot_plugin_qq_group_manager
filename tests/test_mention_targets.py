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
from src.utils import extract_mention_ids, normalize_command, strip_mention_tokens
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
    ) -> None:
        self.message_str = text
        self._components = list(components or [])
        self.message_obj = SimpleNamespace(
            self_id=self_id,
            raw_message=SimpleNamespace(mentions=list(mentions or []), id=msg_id),
            message_id=msg_id,
            group_id=group_id,
        )

    def get_messages(self):
        return list(self._components)

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def get_self_id(self) -> str:
        return self.message_obj.self_id


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


def test_recall_without_any_message_id_reports_clearly():
    """拿不到 ID 时给明确回执，而不是把无事发生的空串发去平台。"""
    plugin = object.__new__(load_main().QQGroupManager)
    event = FakeEvent("撤回")  # 无引用、无消息 ID
    assert asyncio.run(plugin._cmd_recall(event, GROUP)) == [
        "拿不到这条消息的 ID（引用消息与本条消息都为空），无法撤回。"
    ]


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

