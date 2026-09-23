"""申诉闭环 + 误判自学习白名单测试（B1）。

约定：AuditStore 有队列写协程，全链路用例必须包在同一个 asyncio.run 里；
假件优先（tests/fakes.py），不使用 unittest.mock。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import Reply

from src.actions import ActionExecutor
from src.api_client import QQGroupAPI
from src.audit import DDL_STATEMENTS, AuditStore
from src.moderator import LLMModerator
from src.rules import RuleEngine
from src.store import PluginStore
from src.utils import digest_text, now_ts
from tests.fakes import FakeAudit, FakeKV, FakeTransport

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MUTE_PATH = "/v2/groups/{group_openid}/restrict_chat_setting"
TEXT = "这是一条被误判的正常消息"


def load_main():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


class FakeEvent:
    """模拟 AstrBot 群消息事件（指令 + 审核链路用到的接口）。"""

    def __init__(
        self,
        text: str,
        *,
        group_id: str = "g1",
        sender_id: str = "u1",
        sender_name: str = "小号",
        reply_id: str = "",
        admin: bool = False,
        msg_id: str = "m-cmd",
    ) -> None:
        self.message_str = text
        self._reply = Reply(id=reply_id) if reply_id else None
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(id=msg_id, attachments=[], author=None),
            group_id=group_id,
            message_id=msg_id,
            group=None,
        )
        self.unified_msg_origin = f"platform:GroupMessage:{group_id}"
        self.sent: list[object] = []
        self.llm_flag: bool | None = None
        self._admin = admin
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_messages(self):
        return [self._reply] if self._reply is not None else []

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def is_admin(self) -> bool:
        return self._admin

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def should_call_llm(self, flag: bool) -> None:
        self.llm_flag = flag


def _event_row(event_id: int, *, msg_id: str, ts: int, state: str = "", text: str = "加群") -> dict:
    row = {
        "id": event_id,
        "ts_unix": ts,
        "ts": "2026-01-01T00:00:00+08:00",
        "group_id": "g1",
        "group_name": "测试群",
        "msg_id": msg_id,
        "sender_openid": "u1",
        "sender_name": "小号",
        "verdict": "violation",
        "category": "广告引流",
        "severity": 3,
        "text_excerpt": text,
        "text_digest": digest_text(text),
        "appealed": 0,
        "appeal_state": state,
    }
    if state:
        row["appealed"] = 1
        row["appeal_text"] = "误判"
    return row


def build_service(
    main,
    store: PluginStore,
    audit,
    *,
    transport: FakeTransport | None = None,
    moderator: LLMModerator | None = None,
):
    """构造只装配必要属性的 QQGroupManager（不跑 AstrBot 生命周期）。"""
    transport = transport or FakeTransport({("POST", MUTE_PATH): {}})
    api = QQGroupAPI(transport, audit=audit, dry_run_getter=store.dry_run)
    service = object.__new__(main.QQGroupManager)
    service.store = store
    service.api = api
    service.audit = audit
    service.rules = RuleEngine(store.keywords())
    service.moderator = moderator or LLMModerator(settings_getter=store.settings)
    service.actions = ActionExecutor(api=api, store=store, audit=audit)
    service.logger = logging.getLogger("qqgm-appeal-test")
    service._seen_messages = {}
    service._context_buffer = {}
    service._last_provider_id = ""
    service._platform_id = ""
    service.notifications = []

    async def _notify(kind, payload):
        service.notifications.append((kind, payload))

    service._notify = _notify
    return service


async def make_store(tmp_path, **kwargs) -> AuditStore:
    store = AuditStore(tmp_path / "appeal.db", flush_interval=0.02, **kwargs)
    await store.initialize()
    return store


async def make_plugin_store(**settings) -> PluginStore:
    store = PluginStore(FakeKV())
    await store.load()
    if settings:
        await store.update_settings(settings)
    await store.ensure_group("g1", name="测试群")
    await store.update_group("g1", {"moderation_enabled": True})
    return store


# 1. Reply 关联成功 → 关联到被引用消息对应事件
def test_appeal_reply_links_to_referenced_event():
    async def scenario():
        main = load_main()
        now = now_ts()
        audit = FakeAudit(
            events=[
                _event_row(1, msg_id="m1", ts=now - 100),
                _event_row(2, msg_id="m2", ts=now - 50),
            ]
        )
        store = await make_plugin_store()
        service = build_service(main, store, audit)
        event = FakeEvent("申诉 我发的是竞赛链接", reply_id="m1")
        replies = await main.QQGroupManager._cmd_appeal(
            service, event, "g1", ["我发的是竞赛链接"], "u1", "小号"
        )
        assert replies and "已提交" in replies[0]
        assert audit.events[0]["appeal_state"] == "pending"
        assert audit.events[0]["appeal_text"] == "我发的是竞赛链接"
        assert not audit.events[1].get("appealed")

    asyncio.run(scenario())


# 2. 无 Reply → 回退 find_last_event
def test_appeal_without_reply_falls_back_to_last_event():
    async def scenario():
        main = load_main()
        now = now_ts()
        audit = FakeAudit(
            events=[
                _event_row(1, msg_id="m1", ts=now - 100),
                _event_row(2, msg_id="m2", ts=now - 50),
            ]
        )
        store = await make_plugin_store()
        service = build_service(main, store, audit)
        event = FakeEvent("申诉 误判了")
        replies = await main.QQGroupManager._cmd_appeal(
            service, event, "g1", ["误判了"], "u1", "小号"
        )
        assert replies and "已提交" in replies[0]
        assert audit.events[1]["appeal_state"] == "pending"
        assert not audit.events[0].get("appealed")

    asyncio.run(scenario())


# 3. 已处理记录再申诉 → 提示已处理，不重复开单
def test_appeal_on_processed_event_is_rejected_with_notice():
    async def scenario():
        main = load_main()
        audit = FakeAudit(events=[_event_row(1, msg_id="m1", ts=now_ts() - 10, state="accepted")])
        store = await make_plugin_store()
        service = build_service(main, store, audit)
        event = FakeEvent("申诉 再申诉一次", reply_id="m1")
        replies = await main.QQGroupManager._cmd_appeal(
            service, event, "g1", ["再申诉一次"], "u1", "小号"
        )
        assert replies and "已处理" in replies[0]
        assert audit.appeals == []

    asyncio.run(scenario())


# 4. create_appeal 写 pending；resolve_appeal(accepted) 写 accepted + by + at + note
def test_create_and_resolve_appeal_roundtrip(tmp_path):
    async def scenario():
        audit = await make_store(tmp_path)
        event_id = await audit.insert_event(
            group_id="g1", source="llm", verdict="violation", msg_id="m1",
            sender_openid="u1", text_digest="d1", text_excerpt="加群",
        )
        assert await audit.create_appeal(event_id, "误判")
        row = await audit.get_event(event_id)
        assert row["appealed"] == 1
        assert row["appeal_state"] == "pending"
        assert await audit.resolve_appeal(event_id, accepted=True, by="qq:u1", note="核实通过")
        row = await audit.get_event(event_id)
        assert row["appeal_state"] == "accepted"
        assert row["appeal_by"] == "qq:u1"
        assert row["appeal_at"]
        assert row["appeal_note"] == "核实通过"
        pending = await audit.list_appeals(state="accepted", group_id="g1")
        assert [item["id"] for item in pending] == [event_id]
        await audit.close()

    asyncio.run(scenario())


# 5. resolve_appeal 不覆盖 appeal_text（回归保护）
def test_resolve_appeal_does_not_overwrite_appeal_text(tmp_path):
    async def scenario():
        audit = await make_store(tmp_path)
        event_id = await audit.insert_event(
            group_id="g1", source="llm", verdict="violation", sender_openid="u1"
        )
        await audit.create_appeal(event_id, "原申诉理由")
        await audit.resolve_appeal(event_id, accepted=False, by="webui:admin", note="证据不足")
        row = await audit.get_event(event_id)
        assert row["appeal_text"] == "原申诉理由"
        assert row["appeal_state"] == "rejected"
        await audit.close()

    asyncio.run(scenario())


# 6. 迁移幂等：连跑两次不报错；老库自动补列
def test_initialize_is_idempotent_and_migrates_old_db(tmp_path):
    async def scenario():
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        for statement in DDL_STATEMENTS:
            conn.execute(statement)
        for column in ("appeal_by", "appeal_at", "appeal_note"):
            conn.execute(f"ALTER TABLE mod_events DROP COLUMN {column}")
        conn.execute("DROP TABLE appeal_whitelist")
        conn.commit()
        conn.close()

        audit = AuditStore(db_path, flush_interval=0.02)
        await audit.initialize()
        await audit.initialize()  # 第二次必须无异常
        columns = {row[1] for row in audit._conn.execute("PRAGMA table_info(mod_events)")}
        assert {"appeal_by", "appeal_at", "appeal_note"} <= columns
        tables = {
            row[0]
            for row in audit._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "appeal_whitelist" in tables
        await audit.close()

    asyncio.run(scenario())


# 7. 申诉通过 → 调 unmute_member 且 mod_actions 追加 appeal_accepted
def test_accept_appeal_unmutes_and_records_action(tmp_path):
    async def scenario():
        main = load_main()
        audit = await make_store(tmp_path)
        store = await make_plugin_store(dry_run=False)
        event_id = await audit.insert_event(
            group_id="g1", source="llm", verdict="violation", msg_id="m1",
            sender_openid="u1", sender_name="小号", text_digest="d1", text_excerpt="加群",
        )
        await audit.upsert_mute(
            group_id="g1", member_openid="u1", until_unix=now_ts() + 600,
            reason="广告", source="auto", event_id=event_id, active=True,
        )
        audit.record_action(event_id=event_id, group_id="g1", action="mute", ok=True)
        await audit.flush()
        transport = FakeTransport({("POST", MUTE_PATH): {"trace_id": "t1"}})
        service = build_service(main, store, audit, transport=transport)
        result = await service.appeal_decide(
            event_id, accepted=True, by="webui:admin", note="核实通过"
        )
        assert result["ok"] is True
        assert result["unmuted"] is True
        calls = transport.calls_for("POST", MUTE_PATH)
        assert len(calls) == 1
        assert calls[0]["json"]["members"][0]["op"] == "del"
        row = await audit.get_event(event_id)
        assert row["appeal_state"] == "accepted"
        assert row["appeal_text"] is None  # 未提交过申诉正文，也不该被写脏
        await audit.flush()
        actions = await audit.query_logs("actions", filters={"event_id": event_id})
        assert "appeal_accepted" in {item["action"] for item in actions["items"]}
        assert await audit.list_mutes("g1") == []
        await audit.close()

    asyncio.run(scenario())


# 8. 申诉驳回 → 无解禁调用，状态 rejected
def test_reject_appeal_does_not_unmute(tmp_path):
    async def scenario():
        main = load_main()
        audit = await make_store(tmp_path)
        store = await make_plugin_store(dry_run=False)
        event_id = await audit.insert_event(
            group_id="g1", source="llm", verdict="violation", msg_id="m1",
            sender_openid="u1", text_digest="d1", text_excerpt="加群",
        )
        await audit.upsert_mute(
            group_id="g1", member_openid="u1", until_unix=now_ts() + 600,
            reason="广告", source="auto", event_id=event_id, active=True,
        )
        audit.record_action(event_id=event_id, group_id="g1", action="mute", ok=True)
        await audit.flush()
        transport = FakeTransport({("POST", MUTE_PATH): {}})
        service = build_service(main, store, audit, transport=transport)
        result = await service.appeal_decide(
            event_id, accepted=False, by="webui:admin", note="证据不足"
        )
        assert result["ok"] is True and result["state"] == "rejected"
        assert transport.calls_for("POST", MUTE_PATH) == []
        row = await audit.get_event(event_id)
        assert row["appeal_state"] == "rejected"
        assert await audit.list_mutes("g1")  # 台账仍为生效状态
        await audit.close()

    asyncio.run(scenario())


# 9. appeal_auto_whitelist=false（默认）→ 不加白名单
def test_auto_whitelist_disabled_by_default(tmp_path):
    async def scenario():
        main = load_main()
        audit = await make_store(tmp_path)
        store = await make_plugin_store()
        event_id = await audit.insert_event(
            group_id="g1", source="llm", verdict="violation", msg_id="m1",
            sender_openid="u1", text_digest=digest_text(TEXT), text_excerpt=TEXT,
        )
        service = build_service(main, store, audit)
        await service.appeal_decide(event_id, accepted=True, by="webui:admin", note="误判")
        assert await audit.whitelist_list() == []
        await audit.close()

    asyncio.run(scenario())


# 10. 打开开关 → 白名单写入 + 后续同内容消息跳过送审但仍写审计
def test_auto_whitelist_skips_llm_but_still_audits(tmp_path):
    async def scenario():
        main = load_main()
        audit = await make_store(tmp_path)
        store = await make_plugin_store(
            dry_run=False, appeal_auto_whitelist=True, send_conditions=["all"]
        )
        event_id = await audit.insert_event(
            group_id="g1", source="llm", verdict="violation", msg_id="m1",
            sender_openid="u1", text_digest=digest_text(TEXT), text_excerpt=TEXT,
        )
        llm_calls: list[int] = []

        async def provider_call(request, system_prompt, user_prompt):
            del request, system_prompt, user_prompt
            llm_calls.append(1)
            return '{"verdict":"allow","category":"无","severity":1,"confidence":0.9}'

        moderator = LLMModerator(provider_call, settings_getter=store.settings)
        service = build_service(main, store, audit, moderator=moderator)
        await service.appeal_decide(event_id, accepted=True, by="webui:admin", note="误判")
        entries = await audit.whitelist_list()
        assert len(entries) == 1
        assert entries[0]["digest"] == digest_text(TEXT)

        event = FakeEvent(TEXT, msg_id="m-new")
        await main.QQGroupManager._moderate(
            service,
            event,
            group_id="g1",
            config=store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert llm_calls == []  # 命中白名单，跳过 LLM 送审
        await audit.flush()
        events = await audit.query_logs("events", filters={"verdict": "allow"})
        assert events["total"] == 1
        assert events["items"][0]["category"] == "whitelisted"
        await audit.close()

    asyncio.run(scenario())


# 11. 白名单撤销 → 命中失效
def test_whitelist_revoke_disables_hit(tmp_path):
    async def scenario():
        audit = await make_store(tmp_path)
        await audit.whitelist_add("sha256:abc", "加群", "误判", "webui:admin")
        assert await audit.whitelist_contains("sha256:abc") is True
        assert await audit.whitelist_remove("sha256:abc") is True
        assert await audit.whitelist_contains("sha256:abc") is False
        assert await audit.whitelist_remove("sha256:abc") is False
        await audit.close()

    asyncio.run(scenario())


# 12. 管理台契约：申诉路由与视图 payload 键存在
def test_appeal_web_routes_registered():
    main = load_main()
    web_api = importlib.import_module(f"{PLUGIN_ROOT.name}.src.web_api")

    class FakeContext:
        def __init__(self):
            self.routes = []

        def register_web_api(self, route, handler, methods, desc):
            self.routes.append({"route": route, "methods": methods, "desc": desc})

    class Service:
        context = FakeContext()

    service = Service()
    web_api.WebApi(service).register()
    paths = {item["route"] for item in service.context.routes}
    prefix = "/" + PLUGIN_ROOT.name + "/"
    for expected in ("appeals", "appeals/decide", "appeal_whitelist"):
        assert prefix + expected in paths, expected
    # 版本号必须与 metadata.yaml 一致（避免两处各写各的，改版时漏一处）
    metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    declared = next(
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("version:")
    )
    assert main.VERSION == declared


# 13. summary_by_category / summary_by_group（B2 周报聚合基础）
def test_summary_by_category_and_group(tmp_path):
    async def scenario():
        audit = await make_store(tmp_path)
        first = await audit.insert_event(
            group_id="g1", group_name="一群", source="llm", verdict="violation",
            category="广告引流", sender_openid="u1",
        )
        second = await audit.insert_event(
            group_id="g1", group_name="一群", source="llm", verdict="violation",
            category="广告引流", sender_openid="u2",
        )
        await audit.insert_event(
            group_id="g2", group_name="二群", source="llm", verdict="allow",
            category="无", sender_openid="u3",
        )
        await audit.create_appeal(first, "误判")
        await audit.resolve_appeal(first, accepted=True, by="webui:a")
        await audit.create_appeal(second, "误判")
        await audit.resolve_appeal(second, accepted=False, by="webui:a")

        by_category = await audit.summary_by_category(7)
        ad = next(row for row in by_category if row["category"] == "广告引流")
        assert ad["total"] == 2
        assert ad["violation"] == 2
        assert ad["appeals"] == 2
        assert ad["accepted"] == 1

        by_group = await audit.summary_by_group(7)
        group = next(row for row in by_group if row["group_id"] == "g1")
        assert group["group_name"] == "一群"
        assert group["total"] == 2
        await audit.close()

    asyncio.run(scenario())


# 14. 群管指令「申诉通过 / 申诉驳回」：回复申诉消息处理
def test_group_admin_appeal_decide_commands():
    async def scenario():
        main = load_main()
        store = await make_plugin_store(dry_run=False)

        audit = FakeAudit(events=[_event_row(1, msg_id="m1", ts=now_ts())])
        audit.events[0].update(
            {"appealed": 1, "appeal_state": "pending", "appeal_text": "误判"}
        )
        service = build_service(main, store, audit)
        event = FakeEvent("申诉通过 核实无误", reply_id="m1")
        replies = await main.QQGroupManager._cmd_appeal_decide(
            service, event, "g1", "申诉通过", ["核实无误"], "admin1"
        )
        assert replies and "已通过" in replies[0]
        assert audit.events[0]["appeal_state"] == "accepted"
        assert audit.events[0]["appeal_by"] == "qq:admin1"
        assert event.sent, "申诉通过后应在群里 @申诉人 回执"

        audit2 = FakeAudit(events=[_event_row(2, msg_id="m2", ts=now_ts())])
        audit2.events[0].update(
            {"appealed": 1, "appeal_state": "pending", "appeal_text": "误判"}
        )
        service2 = build_service(main, store, audit2)
        event2 = FakeEvent("申诉驳回 证据不足", reply_id="m2")
        replies2 = await main.QQGroupManager._cmd_appeal_decide(
            service2, event2, "g1", "申诉驳回", ["证据不足"], "admin1"
        )
        assert replies2 and "已驳回" in replies2[0]
        assert audit2.events[0]["appeal_state"] == "rejected"
        assert audit2.events[0]["appeal_note"] == "证据不足"

    asyncio.run(scenario())
