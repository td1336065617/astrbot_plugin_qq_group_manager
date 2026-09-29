"""入群分群规则（跟随全局开关）单测：状态机、解析、隔离、容错。

覆盖需求 docs/入群审批分群规则-需求文档.md 的 AC-1~AC-6 后台侧与 NFR-1/4/5/9/10。
全部走真实 PluginStore + FakeKV（与既有入群用例同款），不引入桩 store。
"""

from __future__ import annotations

import asyncio

import pytest

from src.models import (
    JOIN_GLOBAL_ONLY_KEYS,
    JOIN_GROUP_OVERRIDABLE_KEYS,
    JOIN_PROTECTED_GROUP_KEYS,
    GroupConfig,
    default_settings,
    validate_join_mode_value,
)
from src.store import (
    KEY_GROUPS,
    PluginStore,
    _normalize_overrides_patch,
    normalize_settings,
    snapshot_join_overrides,
)
from tests.fakes import FakeKV

LEVEL = "join_min_qq_level"
DAYS = "join_min_account_days"


def run(coro):
    return asyncio.run(coro)


class RecLogger:
    """最小日志替身：只记录调用（用于「失败必须可见」断言）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []

    def warning(self, msg, *args):
        self.records.append(("warning", (msg, *args)))

    def info(self, msg, *args):
        self.records.append(("info", (msg, *args)))

    def error(self, msg, *args):
        self.records.append(("error", (msg, *args)))

    def debug(self, msg, *args):
        self.records.append(("debug", (msg, *args)))

    def texts(self, level: str) -> list[str]:
        out = []
        for record_level, record in self.records:
            if record_level != level:
                continue
            msg, *args = record
            try:
                out.append(str(msg) % tuple(args) if args else str(msg))
            except (TypeError, ValueError):
                out.append(f"{msg} {' '.join(map(str, args))}")
        return out


def make_store(*, groups: tuple[str, ...] = ("g1",), logger: RecLogger | None = None):
    store = PluginStore(FakeKV(), logger=logger)
    run(store.load())
    for gid in groups:
        run(store.ensure_group(gid, name=gid))
    return store


# --------------------------------------------------------------------------
# D1 默认跟随 / 新群无死配置
# --------------------------------------------------------------------------
def test_store_new_group_starts_following_without_config():
    """AC-1：新登记的群默认跟随全局，且**不预生成**配置文件。"""
    store = make_store(groups=())
    config = run(store.ensure_group("fresh", name="新群"))
    assert config.join_follow_global is True
    assert config.join_overrides == {}
    # 无配置 + 开态 → effective 逐键等于全局
    assert store.effective_join_settings("fresh") == store.settings()


def test_effective_join_settings_empty_config_matches_settings():
    """NFR-5 等价基线：默认态下生效配置与全局逐键一致。"""
    store = make_store()
    effective = store.effective_join_settings("g1")
    global_settings = store.settings()
    for key in JOIN_GROUP_OVERRIDABLE_KEYS:
        assert effective[key] == global_settings[key]


# --------------------------------------------------------------------------
# D3 关闭 = 全量固化 / 复活不刷新
# --------------------------------------------------------------------------
def test_store_close_switch_snapshots_current_global_values():
    """AC-2 前半：首次关闭以当时全局固化，生效值在关闭前后不变。"""
    store = make_store()
    run(store.update_settings({LEVEL: 3, "join_expected_answer": "ACM"}))
    before = store.effective_join_settings("g1")
    config = run(store.set_group_follow_global("g1", False))
    assert config.join_follow_global is False
    assert sorted(config.join_overrides) == sorted(JOIN_GROUP_OVERRIDABLE_KEYS)
    after = store.effective_join_settings("g1")
    for key in JOIN_GROUP_OVERRIDABLE_KEYS:
        assert before[key] == after[key]
        assert config.join_overrides[key] == store.get_setting(key)


def test_store_close_switch_revives_existing_config_without_refresh():
    """AC-2 后半：开 → 改全局 → 再关，复活的是原配置（不是当前全局）。"""
    store = make_store()
    run(store.update_settings({LEVEL: 3}))
    run(store.set_group_follow_global("g1", False))          # 固化 level=3
    run(store.update_settings({LEVEL: 9}))                   # 全局改为 9
    run(store.set_group_follow_global("g1", True))           # 开：配置休眠
    assert store.effective_join_settings("g1")[LEVEL] == 9
    config = run(store.set_group_follow_global("g1", False)) # 再关：复活 3
    assert config.join_overrides[LEVEL] == 3
    assert store.effective_join_settings("g1")[LEVEL] == 3


def test_store_open_switch_keeps_config_dormant():
    """D2 门控：开态下休眠配置绝不泄漏进生效值。"""
    store = make_store()
    run(store.set_group_follow_global("g1", False))
    run(store.update_join_overrides("g1", {LEVEL: 42}))       # 关态固化一个显眼值
    run(store.update_settings({LEVEL: 1}))                    # 全局改为 1
    run(store.set_group_follow_global("g1", True))            # 开
    effective = store.effective_join_settings("g1")
    assert effective[LEVEL] == 1                              # 取全局，不是 42
    scope = store.join_settings_scope("g1")
    assert scope["follow_global"] is True
    assert LEVEL in scope["own_keys"]                         # 配置仍保留（休眠）
    # 再关 → 42 复活
    run(store.set_group_follow_global("g1", False))
    assert store.effective_join_settings("g1")[LEVEL] == 42


def test_switch_never_deletes_config_keys():
    """NFR-10：开→关、关→开循环都不得删除任何配置键。"""
    store = make_store()
    run(store.set_group_follow_global("g1", False))
    snapshot = dict(store.group("g1").join_overrides)
    for follow in (True, False, True, False):
        run(store.set_group_follow_global("g1", follow))
    assert store.group("g1").join_overrides == snapshot


# --------------------------------------------------------------------------
# FR-4 恢复跟随 / 再固化 + AC-4
# --------------------------------------------------------------------------
def test_store_restore_field_follows_global_and_edit_refreezes():
    store = make_store()
    run(store.set_group_follow_global("g1", False))
    run(store.update_join_overrides("g1", clear=[LEVEL]))     # 恢复跟随
    run(store.update_settings({LEVEL: 5}))
    assert store.effective_join_settings("g1")[LEVEL] == 5    # 跟随全局
    scope = store.join_settings_scope("g1")
    assert LEVEL in scope["restored_keys"] and LEVEL not in scope["own_keys"]
    run(store.update_join_overrides("g1", {LEVEL: 7}))        # 再编辑即固化
    run(store.update_settings({LEVEL: 9}))
    assert store.effective_join_settings("g1")[LEVEL] == 7    # 停止跟随


# --------------------------------------------------------------------------
# NFR-9 同值固化合法（意图不靠值推断）
# --------------------------------------------------------------------------
def test_store_same_value_own_value_survives_global_change():
    """AC-5：关闭瞬间固化值 == 全局值是常态；此后改全局不跟随、不被清除。"""
    store = make_store()                                       # 默认 level=0
    run(store.set_group_follow_global("g1", False))            # 固化 level=0（同值）
    assert LEVEL in store.join_settings_scope("g1")["own_keys"]
    run(store.update_settings({LEVEL: 9}))
    assert store.effective_join_settings("g1")[LEVEL] == 0     # 不跟随
    # 再次保存其它键不得清除同值固化项
    run(store.update_join_overrides("g1", {DAYS: 30}))
    assert LEVEL in store.join_settings_scope("g1")["own_keys"]
    assert store.effective_join_settings("g1")[LEVEL] == 0


# --------------------------------------------------------------------------
# NFR-4 多群隔离
# --------------------------------------------------------------------------
def test_store_isolation_other_group_unchanged():
    """NFR-4：对 A 关开关/改键，B 的生效值不受影响；改全局只影响跟随中的群。"""
    store = make_store(groups=("g1", "g2"))
    run(store.set_group_follow_global("g1", False))           # g1 固化（level=0, days=0）
    frozen = dict(store.group("g1").join_overrides)
    before_g2 = store.effective_join_settings("g2")
    run(store.update_join_overrides("g1", {LEVEL: 10}))
    run(store.update_settings({DAYS: 60}))
    after_g2 = store.effective_join_settings("g2")
    # g2（开态）逐键取全局：level 未变、days 跟随到 60
    assert after_g2[LEVEL] == before_g2[LEVEL] == 0
    assert after_g2[DAYS] == 60
    # g1 固化表除显式写入的 LEVEL 外原样保留，DAYS 仍是固化时刻的 0（不跟随）
    assert store.effective_join_settings("g1")[LEVEL] == 10
    assert store.effective_join_settings("g1")[DAYS] == frozen[DAYS] == 0


def test_isolation_group_frozen_values_unchanged_by_global_edit():
    store = make_store(groups=("g1", "g2"))
    run(store.set_group_follow_global("g1", False))           # g1 固化（含 DAYS=0）
    before = dict(store.group("g1").join_overrides)
    run(store.update_settings({LEVEL: 11, DAYS: 45}))
    after = store.group("g1").join_overrides
    assert after == before                                     # 固化表本身不被全局改动
    assert store.effective_join_settings("g1")[LEVEL] == before[LEVEL]
    assert store.effective_join_settings("g2")[LEVEL] == 11    # g2 跟随


# --------------------------------------------------------------------------
# 防御路径（失败必须可见）
# --------------------------------------------------------------------------
def test_store_update_group_rejects_protected_keys():
    store = make_store()
    for key in JOIN_PROTECTED_GROUP_KEYS:
        with pytest.raises(ValueError):
            run(store.update_group("g1", {key: {}}))
    # 其它键照常可写
    run(store.update_group("g1", {"mode": "standard"}))
    assert store.group("g1").mode == "standard"


def test_store_set_follow_global_requires_registered_group():
    store = make_store()
    with pytest.raises(ValueError):
        run(store.set_group_follow_global("ghost", False))
    with pytest.raises(ValueError):
        run(store.update_join_overrides("ghost", {LEVEL: 1}))


def test_store_clear_and_patch_same_key_rejected():
    store = make_store()
    run(store.set_group_follow_global("g1", False))
    with pytest.raises(ValueError):
        run(store.update_join_overrides("g1", {LEVEL: 5}, clear=[LEVEL]))


def test_store_update_join_overrides_rejects_unknown_key():
    store = make_store()
    run(store.set_group_follow_global("g1", False))
    with pytest.raises(ValueError) as info:
        run(store.update_join_overrides("g1", {"bogus_key": 1}))
    assert "bogus_key" in str(info.value)
    with pytest.raises(ValueError) as info:
        run(store.update_join_overrides("g1", clear=["join_poll_interval"]))
    assert "join_poll_interval" in str(info.value)


def test_normalize_overrides_patch_uses_global_normalization():
    """归一化与全局同管线：越界钳制、类型纠正结果一致。"""
    patch = _normalize_overrides_patch(
        {LEVEL: 9999, "join_profile_enabled": 1, "join_answer_keywords": [" a ", "", "b"]},
        normalize_settings({}),
    )
    assert patch[LEVEL] == 144                      # 与全局 NUMERIC_BOUNDS 同上限
    assert patch["join_profile_enabled"] is True    # 与全局 bool 归一一致
    assert patch["join_answer_keywords"] == ["a", "b"]


def test_snapshot_matches_current_global_values():
    store = make_store()
    run(store.update_settings({LEVEL: 3, "join_answer_action": "decline"}))
    snapshot = snapshot_join_overrides(store.settings())
    assert sorted(snapshot) == sorted(JOIN_GROUP_OVERRIDABLE_KEYS)
    assert snapshot[LEVEL] == 3
    assert snapshot["join_answer_action"] == "decline"


def test_load_logs_warning_for_dirty_overrides():
    """手改 KV 的脏数据：归一 + 留痕（失败必须可见）。"""
    kv = FakeKV()
    kv.data[KEY_GROUPS] = {
        "g1": {
            "group_id": "g1",
            "join_follow_global": "yes",              # 非布尔
            "join_overrides": "oops",                 # 非 dict
        },
        "g2": {
            "group_id": "g2",
            "join_overrides": {LEVEL: 5, "bogus_key": 1},  # 含非法键
        },
    }
    logger = RecLogger()
    store = PluginStore(kv, logger=logger)
    run(store.load())
    g1, g2 = store.group("g1"), store.group("g2")
    assert g1.join_follow_global is True and g1.join_overrides == {}
    assert g2.join_overrides == {LEVEL: 5}            # 非法键被过滤
    warnings = "\n".join(logger.texts("warning"))
    assert "join_follow_global" in warnings
    assert "join_overrides 结构非法" in warnings
    assert "bogus_key" in warnings


# --------------------------------------------------------------------------
# 作用域标签（FR-10 留痕三态）与影响面计数（FR-9）
# --------------------------------------------------------------------------
def test_store_scope_label_states():
    store = make_store(groups=("g1", "g2", "g3"))
    assert store.join_scope_label("g1") == "global"          # 开态
    assert store.join_scope_label("missing") == "global"     # 群缺失防御回退
    run(store.set_group_follow_global("g2", False))
    assert store.join_scope_label("g2") == "off"             # 关态全固化
    run(store.set_group_follow_global("g3", False))
    run(store.update_join_overrides("g3", clear=[LEVEL, DAYS]))
    assert store.join_scope_label("g3") == "off|" + ",".join(sorted([LEVEL, DAYS]))


def test_store_follow_stats_matches_effective_source():
    """AC-6：计数按「实际生效源」——开态与关态恢复跟随的键都计入 follow。"""
    store = make_store(groups=("g_open", "g_own", "g_mixed"))
    run(store.set_group_follow_global("g_own", False))       # 17 键全 own
    run(store.set_group_follow_global("g_mixed", False))
    run(store.update_join_overrides("g_mixed", clear=[LEVEL]))  # g_mixed 的 LEVEL 恢复跟随
    stats = store.join_follow_stats()
    assert stats[LEVEL] == {"follow": 2, "own": 1}           # g_open + g_mixed 跟随；g_own 自有
    assert stats[DAYS] == {"follow": 1, "own": 2}            # 只有 g_open 跟随
    total = sum(stats[LEVEL].values())
    assert total == len(store.groups())


# --------------------------------------------------------------------------
# 白名单与容错
# --------------------------------------------------------------------------
def test_whitelist_partition_covers_all_join_settings():
    """三向一致性（后端侧）：join_* 设置 ⊆ 可固化 ∪ 全局唯一，且两集合不相交。"""
    join_keys = {key for key in default_settings() if key.startswith("join_")}
    assert set(JOIN_GROUP_OVERRIDABLE_KEYS) & set(JOIN_GLOBAL_ONLY_KEYS) == set()
    assert join_keys == set(JOIN_GROUP_OVERRIDABLE_KEYS) | set(JOIN_GLOBAL_ONLY_KEYS)
    assert len(JOIN_GROUP_OVERRIDABLE_KEYS) == 17
    assert len(JOIN_GLOBAL_ONLY_KEYS) == 5


def test_group_config_from_dict_tolerates_dirty_shapes():
    """设计 §2.4 四种脏形态：不抛异常、归一到安全方向（跟随全局）。"""
    empty = GroupConfig.from_dict({})
    assert empty.join_follow_global is True and empty.join_overrides == {}
    bad_bool = GroupConfig.from_dict({"join_follow_global": "false"})
    assert bad_bool.join_follow_global is True               # 非布尔 → 跟随全局
    bad_dict = GroupConfig.from_dict({"join_overrides": ["x"]})
    assert bad_dict.join_overrides == {}
    mixed = GroupConfig.from_dict({"join_overrides": {LEVEL: 5, "bogus_key": 1}})
    assert mixed.join_overrides == {LEVEL: 5}
    # 缺字段（旧版本数据）→ 等价于跟随全局，零迁移
    legacy = GroupConfig.from_dict({"group_id": "g", "join_review_mode": "strict"})
    assert legacy.join_follow_global is True and legacy.join_overrides == {}


def test_to_dict_roundtrip_preserves_new_fields():
    kv = FakeKV()
    store = PluginStore(kv)
    run(store.load())
    run(store.ensure_group("g1"))
    run(store.set_group_follow_global("g1", False))
    data = store.group("g1").to_dict()
    assert data["join_follow_global"] is False
    assert data["join_overrides"] == GroupConfig.from_dict(data).join_overrides
    run(store.flush())
    reloaded = PluginStore(kv)  # 同一 KV 重载，验证持久化
    run(reloaded.load())
    assert reloaded.group("g1").join_follow_global is False
    assert reloaded.group("g1").join_overrides == store.group("g1").join_overrides


# --------------------------------------------------------------------------
# 模式校验（FR-12，S3 前置的纯函数）
# --------------------------------------------------------------------------
def test_validate_join_mode_value_allows_empty():
    from src.models import JOIN_REVIEW_MODES

    assert validate_join_mode_value("") == ""
    assert validate_join_mode_value(None) == ""
    for mode in JOIN_REVIEW_MODES:
        assert validate_join_mode_value(mode) == mode
    with pytest.raises(ValueError) as info:
        validate_join_mode_value("bogus")
    assert "跟随全局" in str(info.value)


# --------------------------------------------------------------------------
# S2：消费点改造与判定留痕
# --------------------------------------------------------------------------
REPO_ROOT = __file__.rsplit("/", 2)[0]


def _read(rel_path: str) -> str:
    with open(f"{REPO_ROOT}/{rel_path}", encoding="utf-8") as handle:
        return handle.read()


def _make_reviewer(store):
    from src.api_client import QQGroupAPI
    from src.join_review import JoinReviewer
    from tests.fakes import FakeAudit, FakeTransport

    api = QQGroupAPI(FakeTransport({}))
    audit = FakeAudit()

    async def judge_call(system_prompt, user_prompt):
        return '{"decision":"approve","confidence":0.95,"reason":"信息正常"}'

    reviewer = JoinReviewer(api=api, store=store, audit=audit, judge_call=judge_call)
    return reviewer, audit


def _join_request(request_id: str = "j1", **overrides):
    payload = {
        "join_request_id": request_id,
        "member_openid": "u1",
        "username": "张三",
        "apply_source": "self_apply",
        "risk_tips": "",
        "bot": False,
        "verify_info": {"method": "verify_message", "verify_message": "你好"},
        # 画像：等级 3 —— 低于 g_off 的固化门槛 10，但高于全局门槛 0
        "profile": {
            "kind": "onebot",
            "source": "onebot_stranger",
            "degraded": False,
            "qq_level": 3,
            "account_age_days": 500,
        },
    }
    payload.update(overrides)
    return payload


def test_join_review_uses_group_effective_settings():
    """AC-1/AC-3 端到端：同一份申请，关态群按本群门槛拒绝，开态群按全局放行。"""
    store = make_store(groups=("g_off", "g_open"))
    run(store.set_group_follow_global("g_off", False))          # g_off 固化（level=0）
    run(store.update_join_overrides("g_off", {LEVEL: 10}))      # 本群门槛提到 10
    assert store.effective_join_settings("g_open")[LEVEL] == 0  # g_open 仍跟随全局

    reviewer, _audit = _make_reviewer(store)
    off_decision = run(reviewer.judge("g_off", _join_request(), mode="standard"))
    assert off_decision.op == "decline" and off_decision.auto is True
    assert off_decision.gate == "qq_level"

    open_decision = run(reviewer.judge("g_open", _join_request("j2"), mode="standard"))
    assert open_decision.op == "approve"

    # 改全局门槛：g_open 立即受影响，g_off 不受影响（多群隔离 + 跟随语义）
    run(store.update_settings({LEVEL: 99}))
    assert store.effective_join_settings("g_open")[LEVEL] == 99
    assert store.effective_join_settings("g_off")[LEVEL] == 10


def test_persist_records_settings_scope():
    """FR-10：落库带作用域标签；store 缺方法时回退 global（防御不抛）。"""
    from src.join_review import JoinDecision
    from tests.fakes import FakeAudit

    store = make_store(groups=("g1", "g2"))
    run(store.set_group_follow_global("g1", False))
    run(store.update_join_overrides("g1", clear=[LEVEL]))       # 关态 + 恢复一个键
    reviewer, audit = _make_reviewer(store)
    decision = JoinDecision(op="approve", auto=True, confidence=0.9)

    run(reviewer._persist("g1", _join_request(), decision, decided_by="pending"))
    run(reviewer._persist("g2", _join_request("j2"), decision, decided_by="pending"))
    assert audit.joins["j1"]["settings_scope"] == "off|" + LEVEL
    assert audit.joins["j2"]["settings_scope"] == "global"

    class StubStore:  # 不具备 join_scope_label 的替身
        pass

    from src.join_review import JoinReviewer

    stub_audit = FakeAudit()
    stub_reviewer = JoinReviewer(api=None, store=StubStore(), audit=stub_audit)
    run(stub_reviewer._persist("gx", _join_request("jx"), decision, decided_by="pending"))
    assert stub_audit.joins["jx"]["settings_scope"] == "global"


def test_record_join_persists_settings_scope_in_real_db(tmp_path):
    """坑 #9：record_join 的 columns 白名单漏加 → 字段被无声丢弃；用真实库守住。"""
    from src.audit import AuditStore

    async def scenario():
        store = AuditStore(tmp_path / "audit.db", flush_interval=0.05)
        await store.initialize()
        await store.record_join(
            join_request_id="j1", group_id="g1", settings_scope="off|" + LEVEL
        )
        row = await store.get_join("j1")
        assert row is not None and row["settings_scope"] == "off|" + LEVEL
        # 不传该字段 → NULL（升级前旧记录语义）
        await store.record_join(join_request_id="j0", group_id="g1")
        old = await store.get_join("j0")
        assert old is not None and old["settings_scope"] is None
        await store.close()

    asyncio.run(scenario())


def test_llm_provider_gate_and_wiring():
    """FR-11：模型按群门控 + 共享回调路径签名契约（坑 #13）。"""
    # ① store 层：开态取全局，关态取本群固化值
    store = make_store()
    run(store.update_settings({"join_llm_provider_id": "global-model"}))
    assert store.effective_join_settings("g1")["join_llm_provider_id"] == "global-model"
    run(store.set_group_follow_global("g1", False))
    run(store.update_join_overrides("g1", {"join_llm_provider_id": "own-model"}))
    assert store.effective_join_settings("g1")["join_llm_provider_id"] == "own-model"
    run(store.set_group_follow_global("g1", True))
    assert store.effective_join_settings("g1")["join_llm_provider_id"] == "global-model"
    run(store.update_settings({"join_llm_provider_id": ""}))
    assert store.effective_join_settings("g1")["join_llm_provider_id"] == ""

    # ② 源码契约：入群链路按群解析后传 provider_override；_llm_call 有 kw-only 参数
    main_py = _read("main.py")
    assert "provider_override: str | None = None" in main_py
    assert "provider_override=provider_override" in main_py
    assert "effective_join_settings(group_id)" in main_py
    # ③ 源码契约：内容审核的共享回调路径不传新参数（行为零变化）
    moderator_py = _read("src/moderator.py")
    assert "self.provider_call(request, system_prompt, user_prompt)" in moderator_py
    assert "provider_override" not in moderator_py


def test_consumer_guard_no_direct_global_reads():
    """消费点守卫：判定链与画像必须走唯一解析入口，禁止直读全局配置。"""
    join_review_py = _read("src/join_review.py")
    assert "self.store.settings()" not in join_review_py, "join_review 不得直读全局配置"
    assert "effective_join_settings(group_id)" in join_review_py

    profiles_py = _read("src/profiles.py")
    # 只允许 join_profile_concurrency（全局闸门键，不在覆盖白名单）一处直读
    assert profiles_py.count("self.store.settings()") == 1
    assert "join_profile_concurrency" in profiles_py
    assert "effective_join_settings(group_id)" in profiles_py


# --------------------------------------------------------------------------
# S3：joins/settings 解析（纯函数）与 API 接线
# --------------------------------------------------------------------------
def test_joins_settings_parser_forms_and_errors():
    from src.web_api import parse_joins_settings_payload

    # —— 三形态 ——
    global_cmd = parse_joins_settings_payload(
        {"join_min_qq_level": 10, "join_expected_answer": "ACM"}
    )
    assert global_cmd.scope == "global" and global_cmd.group_id == ""
    assert global_cmd.follow_global is None and global_cmd.clear == []
    assert global_cmd.patch == {"join_min_qq_level": 10, "join_expected_answer": "ACM"}
    # 全局形态可写 join_review_mode（全局模式本身），但不能带分群控制键
    assert "join_review_mode" in parse_joins_settings_payload(
        {"join_review_mode": "strict"}
    ).patch

    switch_cmd = parse_joins_settings_payload({"group_id": "g1", "follow_global": False})
    assert switch_cmd.scope == "group" and switch_cmd.follow_global is False
    assert switch_cmd.patch == {} and switch_cmd.clear == []

    patch_cmd = parse_joins_settings_payload({"group_id": "g1", LEVEL: 5})
    assert patch_cmd.patch == {LEVEL: 5} and patch_cmd.follow_global is None

    reset_cmd = parse_joins_settings_payload({"group_id": "g1", "reset": [LEVEL]})
    assert reset_cmd.clear == [LEVEL]
    star_cmd = parse_joins_settings_payload({"group_id": "g1", "reset": "*"})
    assert sorted(star_cmd.clear) == sorted(JOIN_GROUP_OVERRIDABLE_KEYS)
    combo_cmd = parse_joins_settings_payload(
        {"group_id": "g1", LEVEL: 5, "reset": [DAYS]}
    )
    assert combo_cmd.patch == {LEVEL: 5} and combo_cmd.clear == [DAYS]

    # —— 六类 ValueError ——
    def rejected(payload, token):
        with pytest.raises(ValueError) as info:
            parse_joins_settings_payload(payload)
        assert token in str(info.value), f"{payload} → {info.value}"
        return str(info.value)

    rejected("not-a-dict", "JSON 对象")                                   # ① 非对象
    rejected({"group_id": "g1", "bogus_key": 1}, "bogus_key")             # ② 未知键
    rejected({"follow_global": True}, "group_id")                         # ③ 分群控制键缺 group_id
    rejected({}, "没有可保存的配置键")                                      # ④ 全局空键集
    rejected(
        {"group_id": "g1", "follow_global": True, "reset": [LEVEL]}, "不能与"
    )                                                                     # ⑤ 互斥
    rejected({"group_id": "g1", "follow_global": "false"}, "布尔")         # ⑥ 非布尔
    rejected({"group_id": "g1", LEVEL: 1, "follow_global": True}, "不能与")
    rejected({"group_id": "g1", "reset": "yes"}, '"*"')                    # ⑦ reset 形态
    rejected({"group_id": "g1", "reset": ["join_poll_interval"]}, "join_poll_interval")
    rejected({"group_id": "g1"}, "没有可保存的配置键")
    # 分群形态不允许写全局独有键（如 join_poll_interval 未在覆盖白名单 → 归入未知键或 reset 错）
    rejected({"group_id": "g1", "join_review_mode": "strict"}, "join_review_mode")


def test_api_wiring_source_contract():
    """handler 依赖模块级 request 全局无法直测 → 关键接线用源码契约守住。"""
    web_api_py = _read("src/web_api.py")
    # 变更事件（设计 §8.2）
    assert '"kind": "join_settings_override"' in web_api_py
    # 影响面与键分类下发（FR-6/FR-9）
    assert '"join_overridable_keys": list(JOIN_GROUP_OVERRIDABLE_KEYS)' in web_api_py
    assert '"join_global_only_keys": list(JOIN_GLOBAL_ONLY_KEYS)' in web_api_py
    assert '"join_follow_stats": store.join_follow_stats()' in web_api_py
    # 模式空串走纯函数校验（FR-12）
    assert "validate_join_mode_value(mode)" in web_api_py
    # 路由描述与实际行为一致（会进指令速查）
    assert "设置某群入群审批模式（空=跟随全局）" in web_api_py
    assert "保存入群审批配置（全局/分群）" in web_api_py

    main_py = _read("main.py")
    assert 'payload["scope"] = self.store.join_settings_scope(group_id)' in main_py


def test_webui_gate_wiring_source_contract():
    """S4 WebUI 接线：总开关、作用域徽标、影响面、模式跟随项、编辑跨渲染暂存。"""
    js = _read("pages/manage/app.js")
    # 总开关（FR-2）与两态文案（FR-8）
    assert "跟随全局配置（不含审批模式）" in js
    assert "全部规则取全局；本群配置已保留" in js
    assert "使用本群配置；仍跟随全局的字段：" in js
    # 关闭语义三分支 toast（D3：生成 / 复活 / 打开保留）
    assert "已以当前全局值生成本群配置" in js
    assert "已启用本群配置" in js
    assert "已切换为跟随全局；本群配置" in js
    # 分群保存只提交 dirty + reset（防「恢复跟随被静默重新固化」）
    assert "Object.assign({ group_id: gid }, dirty)" in js
    assert "payload.reset = resets.slice()" in js
    # 作用域徽标三态 + 恢复跟随 / 撤销恢复（FR-4）
    assert "恢复跟随" in js and "撤销恢复" in js
    assert "本群·停用" in js and "跟随·待保存" in js
    # 影响面（FR-9）
    assert "群跟随 · " in js and "群用本群值" in js
    # 模式「跟随全局」选项（FR-12）
    assert "跟随全局（当前：" in js
    # 编辑跨渲染暂存（防静默丢弃）
    assert "joinEdits" in js
    # 编辑时把该键移出 reset：同键不能既在 patch 又在 reset（否则后端 400）
    assert "entry.reset = entry.reset.filter((item) => item !== key)" in js
    # 就地反馈：首次编辑即更新徽标并显示「放弃修改」
    assert "本群·待保存" in js and "discardBtnNode" in js
    # 空配置提示（关态）
    assert "本群配置为空，所有字段当前跟随全局" in js
    # 本群卡字段集合由后端 scope.overridable_keys 驱动（全局键如模式不会漏进本群卡）
    assert "overridableKeys.forEach" in js


def test_commands_join_scope_text_states():
    """指令「规则来源」三态文案（FR-13）：含 NULL 旧记录语义（空串 → 跟随全局）。"""
    from src.commands import join_scope_text

    assert "跟随全局" in join_scope_text("")
    assert "跟随全局" in join_scope_text("global")
    assert "本群配置" in join_scope_text("off") and "全部固化" in join_scope_text("off")
    text = join_scope_text("off|join_min_qq_level,join_expected_answer")
    assert "本群配置" in text and "2 项仍跟随全局" in text
    # 语义断言：固化数 + 跟随数 == 总数，禁止出现「全部固化又部分跟随」的矛盾文案
    assert "全部固化" not in text, text
    import re as _re

    from src.models import JOIN_GROUP_OVERRIDABLE_KEYS as _K
    m = _re.search(r"(\d+) 项固化，(\d+) 项仍跟随全局", text)
    assert m and int(m.group(1)) + int(m.group(2)) == len(_K), text
    off_text = join_scope_text("off")
    assert f"{len(_K)} 项全部固化" in off_text, off_text


def test_webui_p1_maintenance_and_history_scope():
    """S5：重建 / 清空按钮 + 历史表规则来源列（FR-14 / FR-13）。"""
    js = _read("pages/manage/app.js")
    assert "以当前全局值重建" in js and "清空本群配置" in js
    assert "reset: '*' " in js or "reset: '*'" in js          # 清空走 reset:"*"
    assert "overridableKeys.forEach((key) => { payload[key] = settings[key]; })" in js  # 重建取当前全局值
    assert "function fmtJoinScope" in js
    assert "'规则来源'" in js                                  # 历史表列头
    assert "fmtJoinScope(row.settings_scope)" in js            # 行渲染


def test_persist_to_real_audit_roundtrip_settings_scope(tmp_path):
    """端到端补盲：_persist → 真实审计库 → list_joins 能读回 settings_scope。

    防的是「kwargs 列名写错但 FakeAudit 照单全收 → 历史表恒为 NULL」这类契约错位：
    FakeAudit 与真实库必须串起来才暴露。
    """
    from src.audit import AuditStore
    from src.join_review import JoinDecision

    async def scenario():
        audit = AuditStore(tmp_path / "e2e.db", flush_interval=0.05)
        await audit.initialize()
        store = PluginStore(FakeKV())                            # 场景内用 await（不能 asyncio.run 嵌套）
        await store.load()
        await store.ensure_group("g1", name="g1")
        await store.ensure_group("g2", name="g2")
        await store.set_group_follow_global("g1", False)         # g1 自治
        reviewer, _ = _make_reviewer(store)
        reviewer.audit = audit                                    # 接真实审计库
        decision = JoinDecision(op="approve", auto=True, confidence=0.9)

        await reviewer._persist("g1", _join_request(), decision, decided_by="human:tester")
        await reviewer._persist("g2", _join_request("j2"), decision, decided_by="human:tester")

        rows = {row["join_request_id"]: row for row in await audit.list_joins(None, limit=10)}
        assert rows["j1"]["settings_scope"] == "off"              # g1 关态全固化
        assert rows["j2"]["settings_scope"] == "global"           # g2 开态跟随
        assert rows["j1"]["group_id"] == "g1"                     # 其余列未错位
        assert rows["j1"]["decided_by"] == "human:tester"
        await audit.close()

    asyncio.run(scenario())
