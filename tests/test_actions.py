"""ActionExecutor 单元测试：动作规划、降级、幂等、dry-run。"""

from __future__ import annotations

import asyncio

from src.actions import ActionExecutor
from src.api_client import QQGroupAPI
from src.models import (
    CAP_BLACKLIST,
    CAP_MUTE,
    CAP_RECALL,
    CapabilityResult,
    GroupConfig,
    Verdict,
    default_settings,
)
from src.store import PluginStore
from tests.fakes import FakeAudit, FakeKV, FakeTransport

MUTE_PATH = "/v2/groups/{group_openid}/restrict_chat_setting"
RECALL_PATH = "/v2/groups/{group_openid}/messages/{message_id}"


def run(coro):
    return asyncio.run(coro)


def make_env(*, dry_run=False, capabilities=(), transport=None):
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.update_settings({"dry_run": dry_run}))
    api = QQGroupAPI(
        transport or FakeTransport({("POST", MUTE_PATH): {}, ("DELETE", RECALL_PATH): {}}),
        dry_run_getter=store.dry_run,
    )
    audit = FakeAudit()
    api.audit = audit
    actions = ActionExecutor(api=api, store=store, audit=audit)
    config = GroupConfig(group_id="g1", name="测试群")
    for capability in capabilities:
        config.capabilities[capability] = CapabilityResult(capability, True).to_dict()
    return store, api, audit, actions, config


def test_plan_actions_prefers_hard_actions():
    _, _, _, actions, _ = make_env()
    settings = default_settings()
    verdict = Verdict(verdict="violation", severity=3, category="广告引流", confidence=0.9)
    matrix_actions = actions.plan_actions(verdict=verdict, settings=settings)
    assert matrix_actions == ["warn", "mute"]  # 标准档矩阵（severity=3）
    hard = actions.plan_actions(verdict=verdict, settings=settings, hard_actions=["recall", "mute"])
    assert hard == ["recall", "mute"]


def test_lenient_mode_only_warns_and_records_event():
    store, _, audit, actions, config = make_env(capabilities=(CAP_RECALL, CAP_MUTE))
    run(store.update_settings({"mode": "lenient"}))
    verdict = Verdict(
        verdict="violation", severity=5, category="广告引流", reason="含链接", confidence=0.9
    )
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    result = run(
        actions.handle(
            group_id="g1",
            config=config,
            verdict=verdict,
            settings=store.settings(),
            msg_id="m1",
            sender_openid="u1",
            sender_name="张三",
            send=send,
        )
    )
    assert audit.events and audit.events[0]["verdict"] == "violation"
    # lenient 模式只保留 warn/report（矩阵里的 recall/mute 被过滤）
    assert [item["action"] for item in result["actions"]] == ["warn", "report"]
    assert len(sent) == 1 and "审核" in sent[0]  # lenient 模式会真正发出警告
    assert audit.actions[0]["ok"] is True


def test_standard_mode_executes_recall_and_mute():
    store, api, audit, actions, config = make_env(
        capabilities=(CAP_RECALL, CAP_MUTE), dry_run=False
    )
    run(store.update_settings({"mode": "standard"}))
    verdict = Verdict(
        verdict="violation", severity=4, category="广告引流", reason="含链接", confidence=0.95
    )
    result = run(
        actions.handle(
            group_id="g1",
            config=config,
            verdict=verdict,
            settings=store.settings(),
            msg_id="m2",
            sender_openid="u1",
            send=None,
        )
    )
    executed = [item["action"] for item in result["actions"]]
    assert executed == ["recall", "mute"]
    assert all(item["ok"] for item in result["actions"])
    mutes = run(audit.list_mutes("g1"))
    assert mutes and mutes[0]["member_openid"] == "u1"
    api_calls = [call["path"] for call in api.transport.calls]
    assert RECALL_PATH in api_calls and MUTE_PATH in api_calls


def test_missing_capability_degrades():
    store, api, _audit, actions, config = make_env(dry_run=False)
    run(store.update_settings({"mode": "standard"}))
    verdict = Verdict(verdict="violation", severity=5, category="广告引流", confidence=0.99)
    result = run(
        actions.handle(
            group_id="g1",
            config=config,
            verdict=verdict,
            settings=store.settings(),
            msg_id="m3",
            sender_openid="u1",
            send=None,
        )
    )
    by_action = {item["action"]: item for item in result["actions"]}
    assert by_action["recall"]["ok"] is False
    assert "群管理员" in by_action["recall"]["message"]
    assert by_action["mute"]["ok"] is False
    assert actions.stats["downgraded"] >= 2
    assert api.transport.calls == []  # 能力不可用时不调用平台接口


def test_idempotent_on_duplicate_message():
    store, api, _audit, actions, config = make_env(capabilities=(CAP_RECALL,), dry_run=False)
    run(store.update_settings({"mode": "standard"}))
    verdict = Verdict(verdict="violation", severity=4, category="广告引流", confidence=0.99)
    for _ in range(2):
        run(
            actions.handle(
                group_id="g1",
                config=config,
                verdict=verdict,
                settings=store.settings(),
                msg_id="m4",
                sender_openid="u1",
                send=None,
            )
        )
    recall_calls = [call for call in api.transport.calls if call["path"] == RECALL_PATH]
    assert len(recall_calls) == 1  # 第二次被判为重复推送


def test_blacklist_falls_back_to_local_when_capability_missing():
    store, _, _audit, actions, config = make_env(dry_run=False)
    result = run(
        actions.handle(
            group_id="g1",
            config=config,
            verdict=Verdict(verdict="violation", severity=5, category="广告引流"),
            settings={
                **store.settings(),
                "mode": "standard",
                "action_matrix": {"violation": {"5": ["blacklist"]}},
            },
            msg_id="m5",
            sender_openid="u1",
            send=None,
        )
    )
    assert result["actions"][0]["ok"] is True
    assert "本地黑名单" in result["actions"][0]["message"]
    assert store.local_blacklist("g1") == ["u1"]
    assert CAP_BLACKLIST not in config.capabilities


def test_log_only_mode_skips_all_actions():
    store, _, audit, actions, config = make_env(capabilities=(CAP_RECALL, CAP_MUTE))
    run(store.update_settings({"mode": "log_only"}))
    result = run(
        actions.handle(
            group_id="g1",
            config=config,
            verdict=Verdict(verdict="violation", severity=5, category="广告引流"),
            settings=store.settings(),
            msg_id="m6",
            sender_openid="u1",
            send=None,
        )
    )
    assert result["actions"] == []
    assert audit.events  # 仍然记录事件

# —— 迁移自已删除的旧机制测试文件：仍需守住的两条批次1行为（H1）——

def executor() -> ActionExecutor:
    return ActionExecutor(api=None, store=None)

def review_verdict(stage: str = "judged", category: str = "色情低俗") -> Verdict:
    return Verdict(verdict="review", category=category, severity=2, confidence=0.9, stage=stage)

def test_default_settings_never_produce_actions_for_review_or_allow():
    """A3 之后：真判定的 review 走既有 report 出口（severity>=2）；allow 与 judged_kv 仍不产生动作。"""
    settings = dict(default_settings())
    ex = executor()
    assert ex.plan_actions(verdict=review_verdict(), settings=settings) == ["report"]
    assert ex.plan_actions(verdict=review_verdict(stage="judged_kv"), settings=settings) == []
    assert ex.plan_actions(verdict=Verdict(verdict="allow", category="无"), settings=settings) == []
    kv_violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.99, stage="judged_kv")
    assert ex.plan_actions(verdict=kv_violation, settings=settings) == []

def test_lenient_mode_does_not_act_on_judged_kv_violation():
    """lenient 会给 violation 补 warn，但 judged_kv（KV 兜底）必须例外。"""
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    class _Api:
        def dry_run(self) -> bool:
            return False

    settings = dict(default_settings())
    settings.update({"mode": "lenient", "action_matrix": {"violation": {"4": ["warn", "recall"]}}})
    config = GroupConfig(group_id="g1", name="测试群", mode="lenient")
    ex = ActionExecutor(api=_Api(), store=None)

    kv_violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.99, stage="judged_kv")
    run(ex.handle(group_id="g1", config=config, verdict=kv_violation, settings=settings, send=send))
    assert sent == []

    judged_violation = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.99, stage="judged")
    run(ex.handle(group_id="g1", config=config, verdict=judged_violation, settings=settings, send=send))
    assert sent, "正常 judged 的 violation 在 lenient 下仍应补一条 warn"

def test_review_with_hard_actions_still_reports():
    """review 命中硬规则时，除硬动作外必须补一条上报（地板移除后仍需守住）。"""
    settings = dict(default_settings())
    verdict = Verdict(verdict="review", category="广告引流", severity=2, confidence=0.9, stage="judged")
    actions = executor().plan_actions(verdict=verdict, settings=settings, hard_actions=["warn"])
    assert actions == ["warn", "report"]


def test_low_confidence_caps_actions_to_warn_and_report():
    """A2：低置信只降力度 —— 判定保留，动作封顶为 warn + report。"""
    settings = dict(default_settings())
    ex = executor()
    low = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.5, stage="judged")
    assert ex.plan_actions(verdict=low, settings=settings) == ["warn", "report"]
    high = Verdict(verdict="violation", category="色情低俗", severity=4, confidence=0.95, stage="judged")
    assert ex.plan_actions(verdict=high, settings=settings) == ["recall", "mute"]


def test_review_reports_only_for_judged_and_severity_two_plus():
    """A3：review 只认真判定、且 severity >= 2 才走既有 report 出口。"""
    settings = dict(default_settings())
    ex = executor()
    assert ex.plan_actions(verdict=review_verdict(), settings=settings) == ["report"]
    one = Verdict(verdict="review", category="其他", severity=1, confidence=0.9, stage="judged")
    assert ex.plan_actions(verdict=one, settings=settings) == []
    assert ex.plan_actions(verdict=review_verdict(stage="judged_kv"), settings=settings) == []
    assert ex.plan_actions(verdict=review_verdict(stage="parse_error"), settings=settings) == []

