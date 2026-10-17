"""防刷屏（同一个人连续发相同内容）测试。

覆盖：判定口径（同内容/不同内容/窗口/交替/占比）、档位（2 级警告、3 级禁言）、
与既有处置矩阵的关系（standard/lenient/log_only）、dry-run、豁免、图片指纹、开关。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from src.actions import ActionExecutor
from src.api_client import QQGroupAPI
from src.audit import AuditStore
from src.models import CapabilityResult, Verdict, default_settings
from src.moderator import LLMModerator
from src.rules import RuleEngine, SpamStat
from src.store import PluginStore
from tests.fakes import FakeKV, FakeTransport

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MUTE_PATH = "/v2/groups/{group_openid}/restrict_chat_setting"
RECALL_PATH = "/v2/groups/{group_openid}/messages/{message_id}"
SAME = "哈哈哈哈"


def load_main():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def make_engine(clock=None) -> RuleEngine:
    engine = RuleEngine({})
    if clock is not None:
        engine._clock = clock.monotonic
    return engine


class FakeEvent:
    """只实现审核链路用到的接口（与 test_moderation_chain 同型）。"""

    def __init__(
        self,
        text: str,
        *,
        group_id: str = "g1",
        msg_id: str = "m1",
        admin: bool = False,
        attachments: list | None = None,
        components: list | None = None,
        raw_data: dict | None = None,
    ) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(
                id=msg_id,
                attachments=list(attachments or []),
                author=None,
                raw_data=raw_data,
            ),
            group_id=group_id,
            message_id=msg_id,
            group=None,
        )
        self.unified_msg_origin = f"platform:GroupMessage:{group_id}"
        self.sent: list[object] = []
        self._admin = admin
        self._components = list(components or [])

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "小号"

    def get_messages(self) -> list:
        return list(self._components)

    def is_admin(self) -> bool:
        return self._admin

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def should_call_llm(self, flag: bool) -> None:
        return None


def image_component(url: str):
    return SimpleNamespace(url=url, file="")


async def make_env(tmp_path: Path, *, mode: str = "standard", dry_run: bool = False, settings=None):
    main = load_main()
    store = PluginStore(FakeKV())
    await store.load()
    patch = {"dry_run": dry_run, "mode": mode, "llm_min_confidence": 0.5}
    patch.update(settings or {})
    await store.update_settings(patch)
    await store.ensure_group("g1", name="测试群")
    await store.update_group("g1", {"moderation_enabled": True})
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
    audit = AuditStore(tmp_path / "spam.db", flush_interval=0.02)
    await audit.initialize()
    api = QQGroupAPI(transport, audit=audit, dry_run_getter=store.dry_run)
    actions = ActionExecutor(api=api, store=store, audit=audit)
    service = object.__new__(main.QQGroupManager)
    service.store = store
    service.api = api
    service.audit = audit
    service.actions = actions
    service.rules = make_engine()

    async def provider_call(request, system_prompt, user_prompt):
        del request, system_prompt, user_prompt
        return '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,"reason":"正常"}'

    # 防刷屏之后仍会继续走内容链路（设计如此），这里给一个永远判允许的审核模型
    service.moderator = LLMModerator(provider_call, settings_getter=store.settings)
    service.logger = logging.getLogger("qqgm-spam-test")
    service._seen_messages = {}
    service._context_buffer = {}
    service._last_provider_id = ""
    service._platform_id = ""
    service._spam_cooldown = {}
    service._spam_clock = time.monotonic  # 与 __init__ 一致（测试用 __new__ 装配）
    return main, service, store, audit, transport


async def send_many(
    env,
    count: int,
    *,
    text: str = SAME,
    role: str = "member",
    admin: bool = False,
    attachments: list | None = None,
    components: list | None = None,
    raw_data: dict | None = None,
    sender: str = "u1",
    prefix: str = "m",
):
    """连发 count 条（每条独立 msg_id，避免被去重吃掉）；返回最后一条事件。"""
    main, service, store, _audit, _transport = env
    event = None
    for index in range(count):
        event = FakeEvent(
            text,
            msg_id=f"{prefix}{index}",
            admin=admin,
            attachments=attachments,
            components=components,
            raw_data=raw_data,
        )
        await main.QQGroupManager._moderate(
            service,
            event,
            group_id="g1",
            config=store.group("g1"),
            sender_openid=sender,
            sender_name="小号",
            sender_role=role,
        )
    return event


async def audit_rows(env, kind: str) -> dict:
    _main, _service, _store, audit, _transport = env
    await audit.flush()
    return await audit.query_logs(kind)


# ----------------------------------------------------------------------
# 判定口径（RuleEngine.note_spam）
# ----------------------------------------------------------------------


def test_note_spam_counts_same_item():
    engine = make_engine()
    for _ in range(5):
        stat = engine.note_spam("g1", "u1", "txt:哈哈")
    assert isinstance(stat, SpamStat)
    assert (stat.count, stat.total) == (5, 5)
    assert stat.ratio == 1.0


def test_note_spam_ignores_varied_content():
    engine = make_engine()
    for index in range(5):
        stat = engine.note_spam("g1", "u1", f"txt:内容{index}")
    assert stat.count == 1
    assert stat.total == 5
    assert stat.ratio == 0.2


def test_note_spam_window_slides():
    clock = FakeClock()
    engine = make_engine(clock)
    for _ in range(4):
        engine.note_spam("g1", "u1", "txt:哈哈", window=60.0)
    clock.now = 61.0
    stat = engine.note_spam("g1", "u1", "txt:哈哈", window=60.0)
    assert stat.count == 1, "超出窗口的旧发言必须滑出"


def test_note_spam_alternating_hits_max_group():
    engine = make_engine()
    for key in ("a", "b") * 5:
        stat = engine.note_spam("g1", "u1", f"txt:{key}")
    assert stat.count == 5, "交替刷屏要按最大的同内容组算"
    assert stat.total == 10
    assert stat.ratio == 0.5


def test_note_spam_blank_key_counts_only_toward_ratio():
    """空 key（占位符 / 取不到附件指纹）只计入占比分母，不参与"同内容"计数。"""
    engine = make_engine()
    stat = engine.note_spam("g1", "u1", "")
    assert (stat.count, stat.total) == (0, 1)
    assert stat.ratio == 0.0


def test_note_spam_ratio_uses_all_messages():
    """占比分母 = 该成员窗口内全部发言（含不参与计数的消息）。"""
    engine = make_engine()
    for _ in range(4):
        stat = engine.note_spam("g1", "u1", "txt:哈哈")
    assert (stat.count, stat.total) == (4, 4)
    stat = engine.note_spam("g1", "u1", "")
    assert (stat.count, stat.total) == (4, 5)
    assert abs(stat.ratio - 0.8) < 1e-9


# ----------------------------------------------------------------------
# 档位与处置矩阵
# ----------------------------------------------------------------------


def test_spam_verdict_severity_escalates():
    main = load_main()
    service = object.__new__(main.QQGroupManager)
    low = service._spam_verdict(SpamStat(count=5, total=5, key="txt:x"), threshold=5, window=60)
    high = service._spam_verdict(SpamStat(count=10, total=10, key="txt:x"), threshold=5, window=60)
    assert low.severity == 2 and low.category == "刷屏灌水" and low.source == "rule"
    assert high.severity == 3
    assert "5 条" in low.reason and "100%" in low.reason


def test_plan_actions_follows_matrix_standard():
    env = None  # plan_actions 是纯函数，不需要环境
    del env
    from src.actions import ActionExecutor

    actions = ActionExecutor(api=None, store=None, audit=None)
    settings = default_settings()
    warn = Verdict(verdict="violation", severity=2, category="刷屏灌水", source="rule")
    mute = Verdict(verdict="violation", severity=3, category="刷屏灌水", source="rule")
    assert actions.plan_actions(verdict=warn, settings=settings) == ["warn"]
    assert actions.plan_actions(verdict=mute, settings=settings) == ["warn", "mute"]


def test_mute_seconds_for_severity_three_is_ten_minutes():
    from src.actions import ActionExecutor

    actions = ActionExecutor(api=None, store=None, audit=None)
    assert actions._mute_seconds(default_settings(), 3, 1) == 600


# ----------------------------------------------------------------------
# 链路（_moderate）
# ----------------------------------------------------------------------


def test_chain_warns_at_threshold(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, transport = env
        await send_many(env, 4)
        rows = await audit_rows(env, "events")
        assert rows["total"] == 0, "未达阈值不得有任何动作"
        event = await send_many(env, 1, prefix="z")
        assert len(event.sent) == 1, "第 5 条必须发出警告"
        assert transport.calls_for("POST", MUTE_PATH) == []
        rows = await audit_rows(env, "events")
        assert rows["total"] == 1
        row = rows["items"][0]
        assert row["category"] == "刷屏灌水" and row["severity"] == 2 and row["source"] == "rule"

    asyncio.run(scenario())


def test_chain_mutes_at_double_threshold(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, transport = env
        await send_many(env, 10)
        assert len(transport.calls_for("POST", MUTE_PATH)) == 1, "10 条必须升级为禁言"
        rows = await audit_rows(env, "events")
        assert sorted(row["severity"] for row in rows["items"]) == [2, 3]

    asyncio.run(scenario())


def test_chain_lenient_only_warns(tmp_path):
    async def scenario():
        env = await make_env(tmp_path, mode="lenient")
        _main, _service, _store, _audit, transport = env
        await send_many(env, 10)
        assert transport.calls_for("POST", MUTE_PATH) == [], "lenient 档不得禁言"

    asyncio.run(scenario())


def test_chain_log_only_keeps_audit(tmp_path):
    async def scenario():
        env = await make_env(tmp_path, mode="log_only")
        _main, _service, _store, _audit, transport = env
        event = await send_many(env, 10)
        assert event.sent == [] and transport.calls_for("POST", MUTE_PATH) == []
        rows = await audit_rows(env, "events")
        assert rows["total"] >= 1, "log_only 只审计不动作"

    asyncio.run(scenario())


def test_chain_dry_run_no_mute(tmp_path):
    async def scenario():
        env = await make_env(tmp_path, dry_run=True)
        _main, _service, _store, _audit, transport = env
        await send_many(env, 10)
        assert transport.calls_for("POST", MUTE_PATH) == [], "dry-run 不得禁言"

    asyncio.run(scenario())


def test_chain_skips_exempt_admin(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        event = await send_many(env, 10, admin=True)
        assert event.sent == [], "AstrBot 管理员豁免"
        rows = await audit_rows(env, "events")
        assert rows["total"] == 0, "豁免者不进统计、也不写审计"

    asyncio.run(scenario())


def test_chain_respects_ratio_guard(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        for index in range(15):
            await send_many(env, 1, text=f"不同的话{index}", prefix=f"d{index}")
        event = await send_many(env, 5, prefix="s")
        assert event.sent == [], "聊天很多、偶有重复（占比 < 50%）不该被判刷屏"

    asyncio.run(scenario())


def test_chain_cooldown_suppresses_repeat_warn_but_escalates(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, transport = env
        await send_many(env, 5)
        first = await audit_rows(env, "events")
        assert first["total"] == 1
        # 第 6~9 条：同档（2 级）在冷却期内不重复动作
        await send_many(env, 4, prefix="c")
        second = await audit_rows(env, "events")
        assert second["total"] == 1, "冷却期内不得重复警告"
        # 第 10 条：升档（3 级）必须放行
        await send_many(env, 1, prefix="e")
        assert len(transport.calls_for("POST", MUTE_PATH)) == 1, "升级不受冷却限制"
        third = await audit_rows(env, "events")
        assert third["total"] == 2

    asyncio.run(scenario())


def test_chain_image_fingerprint_hits_even_when_rkey_changes(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        event = None
        for index in range(5):
            # 官方通道形状：raw_data.attachments 带 content_type 与 fileid（rkey 每次都变）
            attachment = {
                "filename": "a.jpg",
                "content_type": "image/jpeg",
                "url": f"https://x/a.jpg?fileid=FILE-A&rkey={index}",
            }
            event = await send_many(
                env,
                1,
                text="",
                attachments=[attachment],
                raw_data={"attachments": [attachment]},
                prefix=f"img{index}",
            )
        assert len(event.sent) == 1, "同一张图（fileid 相同、rkey 变化）连发 5 次要命中"

    asyncio.run(scenario())


def test_chain_image_only_with_review_off_uses_stable_url(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        event = None
        for index in range(5):
            component = image_component(f"https://x/pic.jpg?rkey={index}")
            event = await send_many(
                env, 1, text="", components=[component], prefix=f"c{index}"
            )
        assert len(event.sent) == 1, "纯图片消息（image_review=off）也必须被判刷屏"

    asyncio.run(scenario())


def test_chain_disabled_switch_no_action(tmp_path):
    async def scenario():
        env = await make_env(tmp_path, settings={"spam_flood_enabled": False})
        _main, _service, _store, _audit, _transport = env
        event = await send_many(env, 10)
        assert event.sent == []
        rows = await audit_rows(env, "events")
        assert rows["total"] == 0

    asyncio.run(scenario())


def test_chain_threshold_is_configurable(tmp_path):
    async def scenario():
        env = await make_env(tmp_path, settings={"spam_flood_count": 2})
        _main, _service, _store, _audit, _transport = env
        event = await send_many(env, 2)
        assert len(event.sent) == 1, "阈值改成 2 就应在第 2 条命中"

    asyncio.run(scenario())


def test_spam_item_key_skips_placeholder(tmp_path):
    async def scenario():
        env = await make_env(tmp_path)
        main, service, _store, _audit, _transport = env
        event = FakeEvent("[表情]")
        assert service._spam_item_key(event, "[表情]", []) == ""
        assert service._spam_item_key(event, SAME, []) .startswith("txt:")
        del main

    asyncio.run(scenario())


def test_chain_spam_error_does_not_break_moderation(tmp_path):
    """刷屏判定抛异常时：不得打断内容审核（只 warning），也不能静默。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, service, _store, _audit, _transport = env

        def boom(*args, **kwargs):
            raise RuntimeError("模拟刷屏判定故障")

        service.rules.note_spam = boom
        reached = []

        class Recorder:
            """记录内容审核是否仍然被走到。"""

            def should_send(self, **kwargs):
                del kwargs
                reached.append(True)
                return False

        service.moderator = Recorder()
        event = await send_many(env, 3)
        assert reached == [True, True, True], "刷屏判定失败后仍必须继续走内容审核"
        assert event.sent == [], "判定失败时不得误发警告"

    asyncio.run(scenario())


def test_spam_disabled_keeps_old_flow_identical(tmp_path):
    """开关关闭时：刷屏链路完全不介入（骨架仍由原处计算，去重行为不变）。"""

    async def scenario():
        env = await make_env(tmp_path, settings={"spam_flood_enabled": False})
        _main, service, _store, _audit, _transport = env
        called = []
        original = service.rules.note_spam

        def recorder(*args, **kwargs):
            called.append(True)
            return original(*args, **kwargs)

        service.rules.note_spam = recorder
        for index in range(3):
            await send_many(env, 1, prefix=f"off{index}")
        assert called == [], "开关关闭时不得调用 note_spam"

    asyncio.run(scenario())



def test_spam_item_key_composite_when_text_and_image(tmp_path):
    """同文案 + 不同图片不能聚成一组（否则晒图会被误判刷屏）；同文案 + 同图仍能聚。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, service, _store, _audit, _transport = env
        event = FakeEvent("打卡")
        first = service._spam_item_key(
            event, "打卡", ["https://x/a.jpg?fileid=A&rkey=1"]
        )
        second = service._spam_item_key(
            event, "打卡", ["https://x/b.jpg?fileid=B&rkey=1"]
        )
        again = service._spam_item_key(
            event, "打卡", ["https://x/a.jpg?fileid=A&rkey=2"]
        )
        assert first != second, "同文案不同图必须区分开"
        assert first == again, "同文案同图（rkey 变化）仍应聚成一组"
        assert first.startswith("txt:") and "+file:" in first

    asyncio.run(scenario())


def test_actions_heavier_action_on_same_message_is_not_skipped(tmp_path):
    """同一条消息先被刷屏（3 级）处置、再被判 4 级：更重的处罚必须落地，不能被幂等顶掉。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, service, store, _audit, transport = env
        settings = store.settings()
        config = store.group("g1")

        async def handle(severity):
            return await service.actions.handle(
                group_id="g1",
                config=config,
                verdict=Verdict(
                    verdict="violation",
                    category="刷屏灌水" if severity == 3 else "广告引流",
                    severity=severity,
                    confidence=1.0,
                    source="rule",
                ),
                settings=settings,
                msg_id="same-msg",
                sender_openid="u1",
                sender_name="小号",
                send=None,
            )

        await handle(3)
        assert len(transport.calls_for("POST", MUTE_PATH)) == 1
        await handle(4)
        assert len(transport.calls_for("POST", MUTE_PATH)) == 2, "4 级禁言必须执行"
        # 同严重度重复推送仍被幂等拦住
        await handle(4)
        assert len(transport.calls_for("POST", MUTE_PATH)) == 2, "同档重复推送应被跳过"

    asyncio.run(scenario())


def test_repeat_multiplier_counts_one_message_once(tmp_path):
    """同一条消息被两条链路各处置一次时，重复违规只计一次（否则时长被更快放大）。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, service, store, _audit, _transport = env
        settings = store.settings()
        config = store.group("g1")

        async def handle(msg_id, severity):
            await service.actions.handle(
                group_id="g1",
                config=config,
                verdict=Verdict(verdict="violation", severity=severity, category="刷屏灌水"),
                settings=settings,
                msg_id=msg_id,
                sender_openid="u1",
                sender_name="小号",
                send=None,
            )

        await handle("m1", 2)
        await handle("m1", 3)
        assert len(service.actions._violations[("g1", "u1")]) == 1, "同一条消息只计一次"
        await handle("m2", 2)
        assert len(service.actions._violations[("g1", "u1")]) == 2

    asyncio.run(scenario())



# ----------------------------------------------------------------------
# 交叉验证补测（子代理审计指出的盲区，逐条对应）
# ----------------------------------------------------------------------


def test_note_spam_window_param_respected():
    """窗口参数必须真的生效（用非默认窗口，抓"写死 60 秒"这类回归）。"""
    clock = FakeClock()
    engine = make_engine(clock)
    for _ in range(4):
        engine.note_spam("g1", "u1", "txt:哈哈", window=10.0)
    clock.now = 11.0
    stat = engine.note_spam("g1", "u1", "txt:哈哈", window=10.0)
    assert stat.count == 1, "10 秒窗口内 +11 秒后旧条目必须滑出"


def test_ratio_boundary_exactly_half_still_hits(tmp_path):
    """占比恰好 0.5 必须命中（锁死是 `<` 而不是 `<=`）。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        # 先发 5 条不同内容，再连发 5 条相同：第 10 条时 count=5、total=10，占比恰好 50%
        for index in range(5):
            await send_many(env, 1, text=f"别的话{index}", prefix=f"half-b{index}")
        event = None
        for index in range(4):
            event = await send_many(env, 1, text=SAME, prefix=f"half-a{index}")
        assert event.sent == [], "前 9 条还没到阈值"
        event = await send_many(env, 1, text=SAME, prefix="half-a9")
        assert len(event.sent) == 1, "5/10 = 50% 属于命中（ratio >= 0.5）"

    asyncio.run(scenario())


def test_chain_empty_key_counts_toward_ratio(tmp_path):
    """占位符消息（空 key）要计入占比分母：5 条相同 + 6 条占位符 → 45% 不命中。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        # 占位符必须先发：占比是**逐条增量**评估的——若 5 条相同先到，第 5 条时占比就是 100%
        for index in range(6):
            await send_many(env, 1, text="[表情]", prefix=f"mix-p{index}")
        for index in range(5):
            await send_many(env, 1, text=SAME, prefix=f"mix-a{index}")
        rows = await audit_rows(env, "events")
        # 必须断言审计（而不是"最后一条消息有没有收到警告"）：
        # 若空 key 没计入分母，动作会在第 5 条（更早的事件对象）就发生，断言末条会看不到
        assert rows["total"] == 0, "5/11 < 50%，不该命中（说明空 key 计入了分母）"

    asyncio.run(scenario())


def test_spam_cooldown_zero_disables_cooldown(tmp_path):
    """spam_flood_cooldown=0 表示关闭冷却：同档也要每次都动作。"""

    async def scenario():
        env = await make_env(tmp_path, settings={"spam_flood_cooldown": 0})
        _main, _service, _store, _audit, _transport = env
        await send_many(env, 5)
        first = await audit_rows(env, "events")
        assert first["total"] == 1
        await send_many(env, 1, prefix="nc")
        second = await audit_rows(env, "events")
        assert second["total"] == 2, "冷却关闭时同档应再次动作"

    asyncio.run(scenario())


def test_spam_cooldown_expires_after_duration(tmp_path):
    """冷却到期后同档可以再次动作（时钟可注入，避免"300→1 秒"这类回归测不出）。"""

    async def scenario():
        env = await make_env(tmp_path, settings={"spam_flood_cooldown": 300})
        _main, service, _store, _audit, _transport = env
        fake = FakeClock()
        service._spam_clock = fake.monotonic
        await send_many(env, 5)
        rows = await audit_rows(env, "events")
        assert rows["total"] == 1
        fake.now = 299.0
        assert service._spam_cooldown_ok("g1", "u1", 2, {"spam_flood_cooldown": 300}) is False
        fake.now = 301.0
        assert service._spam_cooldown_ok("g1", "u1", 2, {"spam_flood_cooldown": 300}) is True

    asyncio.run(scenario())


def test_spam_item_key_groups_skeleton_variants(tmp_path):
    """同一文案的变体（插符号/全角）要聚成同一个 key。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, service, _store, _audit, _transport = env
        event = FakeEvent(SAME)
        plain = service._spam_item_key(event, "打卡", [])
        dotted = service._spam_item_key(event, "打·卡", [])
        spaced = service._spam_item_key(event, "打 卡", [])
        assert plain == dotted == spaced, f"骨架变体应同键：{plain!r} / {dotted!r} / {spaced!r}"

    asyncio.run(scenario())


def test_chain_image_component_file_only_is_detected(tmp_path):
    """真实 Image 组件的形状是 file=url / url=""（Image.fromURL），两个分支都要覆盖。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, _store, _audit, _transport = env
        event = None
        for index in range(5):
            component = SimpleNamespace(url="", file=f"https://x/pic.jpg?rkey={index}")
            event = await send_many(
                env, 1, text="", components=[component], prefix=f"fileonly{index}"
            )
        assert len(event.sent) == 1, "file 里带 URL 的组件（真实形状）也必须被判刷屏"

    asyncio.run(scenario())


def test_chain_skips_group_admins_and_trusted(tmp_path):
    """豁免不只 is_admin：群主/群管理员角色与信任名单同样豁免且不进统计。"""

    async def scenario():
        env = await make_env(tmp_path)
        _main, _service, store, _audit, _transport = env
        owner = await send_many(env, 6, role="owner", sender="owner-1", prefix="own")
        assert owner.sent == []
        admin = await send_many(env, 6, role="admin", sender="admin-1", prefix="adm")
        assert admin.sent == []
        # 信任名单走独立 KV 键（store.update_trusted），不是 GroupConfig.trusted
        await store.update_trusted("g1", ["trusted-1"])
        trusted = await send_many(env, 6, sender="trusted-1", prefix="tru")
        assert trusted.sent == []
        rows = await audit_rows(env, "events")
        assert rows["total"] == 0

    asyncio.run(scenario())


def test_spam_flood_enabled_normalized_to_bool():
    """spam_flood_enabled 必须与同类开关一样被归一化成 bool（脏数据不得溜过去）。"""
    from src.store import normalize_settings

    settings = normalize_settings({"spam_flood_enabled": "false"})
    assert isinstance(settings["spam_flood_enabled"], bool)




def test_spam_flood_defaults_are_stable():
    """默认值是对外契约（直接决定生产行为）：5 条 / 60 秒 / 300 秒冷却 / 默认开启。"""
    from src.models import default_settings

    settings = default_settings()
    assert settings["spam_flood_enabled"] is True
    assert settings["spam_flood_count"] == 5
    assert settings["spam_flood_window"] == 60
    assert settings["spam_flood_cooldown"] == 300

