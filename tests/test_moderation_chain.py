"""审核链路集成测试：用假事件跑真实的规则 → LLM → 处置 → 审计全链路。

这一层专门防"方法名写错 / 属性缺失"这类**只在真实运行路径上才暴露**的错误：
生产环境曾出现 PluginStore.member_first_seen 未实现，导致审核链路一进来就抛异常、
群内既不警告也无任何记录（用户侧表现为"没反应"），而当时的单元测试没有覆盖到。

注意：AuditStore 内部有 asyncio 队列与写协程，所有等待必须发生在**同一个事件循环**里，
因此每个用例都用单个 asyncio.run(scenario()) 包住整个流程。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from src.actions import ActionExecutor
from src.api_client import QQGroupAPI
from src.audit import AuditStore
from src.models import CapabilityResult, Verdict
from src.moderator import LLMModerator
from src.rules import RuleEngine
from src.store import PluginStore
from tests.fakes import FakeKV, FakeTransport

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

RECALL_PATH = "/v2/groups/{group_openid}/messages/{message_id}"
MUTE_PATH = "/v2/groups/{group_openid}/restrict_chat_setting"

AD_MESSAGE = "加群领资料 https://example.com/abc"
LLM_VIOLATION = (
    '{"verdict":"violation","category":"广告引流","severity":4,'
    '"confidence":0.95,"reason":"含引流链接","suggested_action":"mute_and_recall"}'
)
LLM_ALLOW = '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,"reason":"正常"}'


def load_main():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


class FakeEvent:
    """模拟 AstrBot 的群消息事件（只实现审核链路用到的接口）。"""

    def __init__(self, text: str, *, group_id: str = "g1", admin: bool = False) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(id="msg-1", attachments=[], author=None),
            group_id=group_id,
            message_id="msg-1",
            group=None,
        )
        self.unified_msg_origin = f"platform:GroupMessage:{group_id}"
        self.sent: list[object] = []
        self.llm_flag: bool | None = None
        self._admin = admin

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "小号"

    def is_admin(self) -> bool:
        return self._admin

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def should_call_llm(self, flag: bool) -> None:
        self.llm_flag = flag


class Chain:
    """一次完整的审核链路运行环境。"""

    def __init__(self, store, audit, event, transport, service, main) -> None:
        self.store = store
        self.audit = audit
        self.event = event
        self.transport = transport
        self.service = service
        self.main = main

    async def close(self) -> None:
        await self.audit.close()

    async def events(self) -> dict:
        await self.audit.flush()
        return await self.audit.query_logs("events")

    async def actions(self) -> dict:
        await self.audit.flush()
        return await self.audit.query_logs("actions")


async def run_chain(
    tmp_path: Path,
    *,
    text: str = AD_MESSAGE,
    mode: str = "lenient",
    dry_run: bool = True,
    llm_response: str | Exception = LLM_VIOLATION,
    admin: bool = False,
    with_capabilities: bool = True,
) -> Chain:
    """在同一个事件循环内跑一次审核链路。"""
    main = load_main()
    store = PluginStore(FakeKV())
    await store.load()
    await store.update_settings({"dry_run": dry_run, "mode": mode, "llm_min_confidence": 0.5})
    await store.ensure_group("g1", name="测试群")
    await store.update_group("g1", {"moderation_enabled": True})
    if with_capabilities:
        await store.set_capabilities(
            "g1",
            {
                "recall": CapabilityResult("recall", True),
                "mute": CapabilityResult("mute", True),
            },
        )

    transport = FakeTransport(
        {("DELETE", RECALL_PATH): {"trace_id": "t1"}, ("POST", MUTE_PATH): {}}
    )
    audit = AuditStore(tmp_path / "audit.db", flush_interval=0.02)
    await audit.initialize()
    api = QQGroupAPI(transport, audit=audit, dry_run_getter=store.dry_run)
    actions = ActionExecutor(api=api, store=store, audit=audit)

    async def provider_call(request, system_prompt, user_prompt):
        del request, system_prompt, user_prompt
        if isinstance(llm_response, Exception):
            raise llm_response
        return llm_response

    moderator = LLMModerator(provider_call, settings_getter=store.settings)

    service = object.__new__(main.QQGroupManager)
    service.store = store
    service.api = api
    service.audit = audit
    service.actions = actions
    service.moderator = moderator
    service.rules = RuleEngine(store.keywords())
    service.logger = logging.getLogger("qqgm-chain-test")
    service._seen_messages = {}
    service._context_buffer = {}
    service._last_provider_id = ""
    service._platform_id = ""

    event = FakeEvent(text, admin=admin)
    await main.QQGroupManager._moderate(
        service,
        event,
        group_id="g1",
        config=store.group("g1"),
        sender_openid="u1",
        sender_name="小号",
        sender_role="member",
    )
    return Chain(store, audit, event, transport, service, main)


def test_chain_records_event_and_warns_in_lenient_dry_run(tmp_path):
    """lenient + dry-run：仍要真的发出警告（非破坏性），但不得撤回/禁言。"""

    async def scenario():
        chain = await run_chain(tmp_path)
        events = await chain.events()
        assert events["total"] == 1
        row = events["items"][0]
        assert row["verdict"] == "violation"
        assert row["category"] == "广告引流"
        assert row["dry_run"] == 1
        assert row["sender_name"] == "小号"
        assert len(chain.event.sent) == 1, "dry-run 期间警告必须真的发出去"
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.transport.calls_for("POST", MUTE_PATH) == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_warn_can_be_silenced_in_dry_run(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path)
        await chain.store.update_settings({"dry_run_warn": False})
        chain.service.actions.stats["skipped"] = 0
        chain.event.sent.clear()
        chain.service._seen_messages.clear()
        await chain.main.QQGroupManager._moderate(
            chain.service,
            chain.event,
            group_id="g1",
            config=chain.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert chain.event.sent == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_standard_mode_executes_recall_and_mute(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, mode="standard", dry_run=False)
        assert len(chain.transport.calls_for("DELETE", RECALL_PATH)) == 1
        assert len(chain.transport.calls_for("POST", MUTE_PATH)) == 1
        actions = await chain.actions()
        assert {"recall", "mute"} <= {item["action"] for item in actions["items"]}
        mutes = await chain.audit.list_mutes("g1")
        assert mutes and mutes[0]["member_openid"] == "u1"
        await chain.close()

    asyncio.run(scenario())


def test_chain_dry_run_blocks_destructive_actions(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, mode="standard", dry_run=True)
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.transport.calls_for("POST", MUTE_PATH) == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_skips_exempt_admin(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, admin=True)
        assert (await chain.events())["total"] == 0
        assert chain.event.sent == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_degrades_when_llm_fails(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, llm_response=RuntimeError("boom"))
        events = await chain.events()
        assert events["total"] == 1
        assert events["items"][0]["verdict"] == "review"
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.event.sent == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_allows_normal_message_without_actions(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, text="大家好呀", llm_response=LLM_ALLOW)
        await chain.events()
        assert chain.event.sent == []
        assert chain.transport.calls == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_duplicate_message_is_processed_once(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path)
        await chain.main.QQGroupManager._moderate(
            chain.service,
            chain.event,
            group_id="g1",
            config=chain.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert (await chain.events())["total"] == 1
        assert len(chain.event.sent) == 1
        await chain.close()

    asyncio.run(scenario())


def test_store_tracks_member_first_seen():
    async def scenario():
        store = PluginStore(FakeKV())
        await store.load()
        assert store.member_first_seen("g1", "u1") is None
        await store.remember_member("g1", "u1", name="小号", role="member")
        first = store.member_first_seen("g1", "u1")
        assert isinstance(first, int) and first > 0
        await store.remember_member("g1", "u1", name="小号二号", role="member")
        assert store.member_first_seen("g1", "u1") == first
        assert store.member_name("g1", "u1") == "小号二号"

    asyncio.run(scenario())


def test_lenient_mode_filters_recall_and_explains(tmp_path):
    """lenient 模式下即使 dry-run 关闭，也只警告，并在文案中说明原因。"""

    async def scenario():
        chain = await run_chain(tmp_path, mode="lenient", dry_run=False)
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.transport.calls_for("POST", MUTE_PATH) == []
        assert len(chain.event.sent) == 1
        chunks = chain.event.sent[0].chain if hasattr(chain.event.sent[0], "chain") else []
        text = "".join(getattr(item, "text", "") for item in chunks)
        assert "宽松模式只警告不撤回/禁言" in text
        await chain.close()

    asyncio.run(scenario())


def test_standard_mode_with_dry_run_explains_limitation(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, mode="standard", dry_run=True)
        assert len(chain.event.sent) == 1
        chunks = chain.event.sent[0].chain if hasattr(chain.event.sent[0], "chain") else []
        text = "".join(getattr(item, "text", "") for item in chunks)
        assert "dry-run" in text
        await chain.close()

    asyncio.run(scenario())


def test_dry_run_command_toggles_setting(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path)
        service = chain.service
        main = load_main()
        off = await main.QQGroupManager._cmd_dry_run(service, ["关闭"])
        assert service.store.dry_run() is False
        assert "真实执行" in off[0]
        on = await main.QQGroupManager._cmd_dry_run(service, ["开启"])
        assert service.store.dry_run() is True
        assert "不会撤回或禁言" in on[0]
        status = await main.QQGroupManager._cmd_dry_run(service, [])
        assert "dry-run" in status[0]
        await chain.close()

    asyncio.run(scenario())


def test_group_mode_override_and_follow_global(tmp_path):
    """群级模式覆盖优先于全局；「审核模式 跟随」应清除覆盖。"""

    async def scenario():
        chain = await run_chain(tmp_path, mode="lenient")
        main = load_main()
        service = chain.service
        # 全局改成标准，但群级仍覆盖为 lenient —— 生效模式应为 lenient
        await service.store.update_settings({"mode": "standard"})
        await service.store.update_group("g1", {"mode": "lenient"})
        text = await main.QQGroupManager._cmd_mode(service, "g1", [])
        assert "宽松" in text[0] and "群级覆盖" in text[0]
        # 切到跟随全局
        reply = await main.QQGroupManager._cmd_mode(service, "g1", ["跟随"])
        assert "跟随全局" in reply[0]
        assert not service.store.group("g1").mode
        text2 = await main.QQGroupManager._cmd_mode(service, "g1", [])
        assert "标准" in text2[0] and "跟随全局" in text2[0]
        await chain.close()

    asyncio.run(scenario())


def test_new_group_follows_global_mode_instead_of_copying():
    """新建群记录不应把当时的全局模式复制成群级覆盖（历史 bug 的回归测试）。"""

    async def scenario():
        from src.store import PluginStore
        from tests.fakes import FakeKV

        store = PluginStore(FakeKV())
        await store.load()
        await store.update_settings({"mode": "strict"})
        await store.ensure_group("g-new", name="新群")
        assert store.group("g-new").mode == "", "群级模式应留空以跟随全局"
        await store.update_settings({"mode": "standard"})
        # 生效模式随之变化
        assert (store.group("g-new").mode or store.get_setting("mode")) == "standard"

    asyncio.run(scenario())


def test_update_keywords_reloads_rule_engine(tmp_path):
    """WebUI/指令保存关键词后必须立即生效（曾因绕过 service 而未热更新）。"""

    async def scenario():
        chain = await run_chain(tmp_path)
        service = chain.service
        # 初始无规则 → 不命中
        assert service.rules.evaluate("违禁词测试", group_id="g1").hard_hits == []
        await service.update_keywords(
            {
                "hard": [
                    {
                        "id": "kw1",
                        "type": "literal",
                        "pattern": "违禁词测试",
                        "action": ["recall", "mute"],
                        "scope": "all",
                        "enabled": True,
                    }
                ],
                "soft": [],
            }
        )
        hits = service.rules.evaluate("违禁词测试", group_id="g1").hard_hits
        assert len(hits) == 1
        assert hits[0].actions == ["recall", "mute"]
        await chain.close()

    asyncio.run(scenario())


def test_webui_keyword_save_goes_through_service():
    """保证 WebUI 的 keywords 保存路径调用 service.update_keywords（会热更新规则引擎）。"""

    source = (PLUGIN_ROOT / "src" / "web_api.py").read_text(encoding="utf-8")
    assert "self.service.update_keywords(" in source, (
        "WebUI 保存关键词必须走 service.update_keywords，否则规则不会热更新"
    )
    assert "await store.update_keywords(" not in source, (
        "不要直接调用 store.update_keywords：它不会 reload 规则引擎"
    )


def test_handler_summary_reports_planned_and_skipped(tmp_path):
    """审核摘要要能说明"计划动作 vs 实际动作"，便于排查"为什么没禁言"。"""

    async def scenario():
        chain = await run_chain(tmp_path, mode="lenient", dry_run=False)
        # lenient 会拦下 recall/mute，只保留 warn
        planned = chain.service.actions.plan_actions(
            verdict=Verdict(verdict="violation", severity=4, category="广告引流"),
            settings=chain.store.settings(),
            hard_actions=["warn", "recall", "mute"],
        )
        assert planned == ["warn", "recall", "mute"]
        await chain.close()

    asyncio.run(scenario())


def test_extract_text_collects_images():
    """图文混排要能同时拿到文本与图片 URL（图片是否送审由配置决定）。"""
    from types import SimpleNamespace

    main = load_main()

    class Image:
        def __init__(self, url):
            self.url = url
            self.file = ""

    event = SimpleNamespace(
        message_str="看这个 https://example.com/abc",
        message_obj=SimpleNamespace(
            raw_message=SimpleNamespace(
                attachments=[{"content_type": "image/png", "url": "https://cdn.example.com/a.png"}],
                id="m1",
            )
        ),
        get_messages=lambda: [Image("https://cdn.example.com/a.png")],
    )
    text, kind, images = main.QQGroupManager._extract_text(event)
    assert "example.com/abc" in text
    assert images == ["https://cdn.example.com/a.png"]
    assert kind == "图文"

    # 纯图片（无组件 URL）也不应报错
    event2 = SimpleNamespace(
        message_str="",
        message_obj=SimpleNamespace(raw_message=SimpleNamespace(attachments=[], id="m2")),
    )
    text2, kind2, images2 = main.QQGroupManager._extract_text(event2)
    assert text2 == "" and images2 == [] and kind2 == "文本"


def test_pure_image_message_review_modes(tmp_path):
    """image_review=off 时纯图片不送审；always 时会把图片交给 LLM。"""

    async def scenario():
        chain = await run_chain(tmp_path, text="", llm_response=LLM_VIOLATION, mode="standard")
        service = chain.service
        main = load_main()

        class Image:
            url = "https://cdn.example.com/a.png"
            file = ""

        class ImageEvent(FakeEvent):
            def __init__(self):
                super().__init__("")
                self.message_obj.raw_message.attachments = [
                    {"content_type": "image/png", "url": "https://cdn.example.com/a.png"}
                ]

            def get_messages(self):
                return [Image()]

        seen: list[list[str]] = []

        async def provider_call(request, system_prompt, user_prompt):
            seen.append(list(request.image_urls))
            assert "图片" in user_prompt or "图片" in system_prompt
            return LLM_VIOLATION

        service.moderator.provider_call = provider_call
        await service.store.update_settings({"image_review": "off"})
        await main.QQGroupManager._moderate(
            service,
            ImageEvent(),
            group_id="g1",
            config=service.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert seen == [], "image_review=off 时不应送审图片"

        await service.store.update_settings({"image_review": "always"})
        service._seen_messages.clear()
        await main.QQGroupManager._moderate(
            service,
            ImageEvent(),
            group_id="g1",
            config=service.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert seen == [["https://cdn.example.com/a.png"]], "image_review=always 应把图片交给 LLM"
        await chain.close()

    asyncio.run(scenario())


def test_with_text_mode_skips_pure_image(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, mode="standard")
        service = chain.service
        main = load_main()
        await service.store.update_settings({"image_review": "with_text"})
        seen: list[list[str]] = []

        async def provider_call(request, system_prompt, user_prompt):
            del system_prompt, user_prompt
            seen.append(list(request.image_urls))
            return LLM_VIOLATION

        service.moderator.provider_call = provider_call

        class Image:
            url = "https://cdn.example.com/a.png"
            file = ""

        class PureImageEvent(FakeEvent):
            def __init__(self):
                super().__init__("")
                self.message_obj.raw_message.attachments = [
                    {"content_type": "image/png", "url": "https://cdn.example.com/a.png"}
                ]

            def get_messages(self):
                return [Image()]

        await main.QQGroupManager._moderate(
            service,
            PureImageEvent(),
            group_id="g1",
            config=service.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert seen == [], "with_text 模式下纯图片消息不送审"
        await chain.close()

    asyncio.run(scenario())


def test_chain_escalates_qr_content_from_image(tmp_path):
    """图片二维码内容命中引流特征时：升级严重度并在审计里留痕。"""

    async def scenario():
        chain = await run_chain(
            tmp_path,
            llm_response=(
                '{"verdict":"review","category":"广告引流","severity":2,'
                '"confidence":0.9,"reason":"疑似引流","suggested_action":"warn",'
                '"qr_text":"加群 987654321"}'
            ),
        )
        events = await chain.events()
        row = events["items"][0]
        assert row["severity"] >= 3, "二维码命中引流特征应升级严重度"
        assert "二维码" in (row["reason"] or "")
        assert "qr_content" in (row["rule_hits"] or "")
        await chain.close()

    asyncio.run(scenario())


def test_chain_without_qr_text_keeps_original_severity(tmp_path):
    async def scenario():
        chain = await run_chain(
            tmp_path,
            llm_response=(
                '{"verdict":"review","category":"广告引流","severity":2,'
                '"confidence":0.9,"reason":"疑似引流","suggested_action":"warn"}'
            ),
        )
        events = await chain.events()
        row = events["items"][0]
        assert row["severity"] == 2
        assert "qr_content" not in (row["rule_hits"] or "")
        await chain.close()

    asyncio.run(scenario())


def test_chain_records_recent_context_and_respects_setting(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, text="第一条：今天比赛真难")
        bucket = chain.service._context_buffer.get("g1") or []
        assert [item["text"] for item in bucket] == ["第一条：今天比赛真难"]
        assert chain.service._recent_context("g1")[-1]["text"] == "第一条：今天比赛真难"
        await chain.store.update_settings({"llm_context_messages": 0})
        assert chain.service._recent_context("g1") == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_passes_recent_context_to_llm(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, text="第一条：今天比赛真难")
        captured = {}

        async def provider_call(request, system_prompt, user_prompt):
            del request, system_prompt
            captured["user"] = user_prompt
            return LLM_ALLOW

        chain.service.moderator.provider_call = provider_call
        chain.service._seen_messages.clear()
        event = FakeEvent(AD_MESSAGE)
        await chain.main.QQGroupManager._moderate(
            chain.service,
            event,
            group_id="g1",
            config=chain.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert "今天比赛真难" in captured.get("user", "")
        assert captured["user"].index("今天比赛真难") < captured["user"].index("<<<MESSAGE")
        await chain.close()

    asyncio.run(scenario())
