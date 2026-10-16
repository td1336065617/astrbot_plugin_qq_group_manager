"""W2-2：地板策略可配（品类差异化 / 冷却 / 每日上限）。"""

from __future__ import annotations

import asyncio

from src.models import CAP_MUTE, CAP_RECALL, Verdict, default_settings
from src.store import normalize_settings
from tests.test_actions import make_env

FLOOR = {"review_floor_actions": ["warn"], "review_floor_categories": []}


def run(coro):
    return asyncio.run(coro)


def _env(extra=None):
    store, _, audit, actions, config = make_env(capabilities=(CAP_RECALL, CAP_MUTE), dry_run=False)
    settings = dict(default_settings())
    settings.update(FLOOR)
    settings.update(extra or {})
    # default_settings() 里 dry_run 默认为 True：地板三闸的用例必须跑在真实执行路径上，
    # 否则 claim=False（dry-run 不占位）会让冷却/上限看起来失效；dry-run 另有专门用例。
    settings["dry_run"] = False
    run(store.update_settings(settings))
    return store, audit, actions, config, store.settings()


def _review(category="色情低俗"):
    return Verdict(verdict="review", category=category, severity=2, confidence=0.4, reason="正文被复述", stage="judged")


def test_plan_actions_uses_category_override():
    store, _, actions, config, settings = _env({"review_floor_by_category": {"色情低俗": ["report"]}})
    assert actions.plan_actions(verdict=_review("色情低俗"), settings=settings) == ["report"]
    assert actions.plan_actions(verdict=_review("辱骂攻击"), settings=settings) == ["warn"]


def test_normalize_floor_by_category_drops_dirty_entries():
    settings = normalize_settings({
        "review_floor_by_category": {
            "色情低俗": ["report", "mute", 5],
            "不在轨道": ["warn"],
            "辱骂攻击": "warn",
        }
    })
    assert settings["review_floor_by_category"] == {"色情低俗": ["report"]}


def test_floor_cooldown_suppresses_second_warn():
    store, _, actions, config, settings = _env({"review_floor_cooldown_seconds": 600})
    clock = {"now": 1000.0}
    actions._clock = lambda: clock["now"]
    first = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m1", sender_openid="u1", send=None))
    assert first["actions"], "首次应给出地板动作"
    second = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m2", sender_openid="u1", send=None))
    assert second["actions"] == [], "冷却期内同成员不再重复"
    clock["now"] += 601
    third = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m3", sender_openid="u1", send=None))
    assert third["actions"], "冷却结束后应恢复"


def test_floor_daily_limit_suppresses_after_cap():
    store, _, actions, config, settings = _env({"review_floor_daily_limit": 1})
    first = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m1", sender_openid="u1", send=None))
    assert first["actions"]
    second = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m2", sender_openid="u2", send=None))
    assert second["actions"] == [], "达到当日上限后不再给地板动作"


def test_floor_defaults_off_keeps_behaviour():
    store, _, actions, config, settings = _env()
    result = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m1", sender_openid="u1", send=None))
    assert [item["action"] for item in result["actions"]] == ["warn"], "默认（无冷却/无上限）行为不变"


def test_floor_day_key_uses_cn_timezone():
    "跨天复位点必须与插件统一口径（北京）一致，不能跟主机时区走。"
    """把进程时区改成 UTC 再断言：否则在 +08 主机上测试会「因为跑在对的机器上」而假绿。"""
    import os
    import time as time_module
    from datetime import datetime, timezone

    from src.actions import floor_day_key

    stamp = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc).timestamp()  # 北京 10-17 01:00
    same_day = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc).timestamp()  # 北京 10-16 17:00

    before = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time_module.tzset()
    try:
        assert floor_day_key(stamp) == "2026-10-17", "地板日期必须按北京日期算（UTC 主机上最容易踩）"
        assert floor_day_key(same_day) == "2026-10-16"
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time_module.tzset()


def test_floor_by_category_ignores_non_warn_report_actions():
    "品类差异化里混入 recall/mute 时必须被丢弃，不能绕过白名单。"
    _, _, _, _, settings = _env({"review_floor_by_category": {"色情低俗": ["mute"]}})
    assert settings["review_floor_by_category"] == {}, "只含非法动作的品类应被整条丢弃"


def test_dry_run_does_not_consume_floor_quota():
    "演练模式不占位：否则试跑一次就把真实提醒的冷却/额度顶掉。"
    store, _, actions, config, settings = _env({"review_floor_cooldown_seconds": 600})
    settings = dict(settings)
    settings["dry_run"] = True
    run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m1", sender_openid="u1", send=None))
    assert actions._floor_seen == {}, "dry-run 不得写冷却占位"
    settings["dry_run"] = False
    result = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m2", sender_openid="u1", send=None))
    assert result["actions"], "真实执行不受 dry-run 影响"


def test_floor_suppression_is_recorded_for_audit():
    "被冷却拦住时必须留下可查的痕迹（否则事后无法区分「被拦住」与「本来无需动作」）。"
    store, _audit, actions, config, settings = _env({"review_floor_cooldown_seconds": 600})
    run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m1", sender_openid="u1", send=None))
    result = run(actions.handle(group_id="g1", config=config, verdict=_review(), settings=settings, msg_id="m2", sender_openid="u1", send=None))
    assert result["actions"] == []
    rows = [row for row in getattr(_audit, "capabilities", []) if row.get("capability") == "review_floor_suppressed"]
    assert rows, "抑制必须写 capability_log"
    assert rows[-1].get("err_code") == "cooldown"


def test_concurrent_same_member_only_one_floor_action():
    "B-1 回归：同一成员两条消息并发时冷却必须真的生效（检查与占位之间不能有 await）。"
    store, _audit, actions, config, settings = _env({"review_floor_cooldown_seconds": 600})

    async def both():
        return await asyncio.gather(
            actions.handle(
                group_id="g1",
                config=config,
                verdict=_review(),
                settings=settings,
                msg_id="m1",
                sender_openid="u1",
                send=None,
            ),
            actions.handle(
                group_id="g1",
                config=config,
                verdict=_review(),
                settings=settings,
                msg_id="m2",
                sender_openid="u1",
                send=None,
            ),
        )

    first, second = run(both())
    given = [item["action"] for item in first["actions"]]
    given += [item["action"] for item in second["actions"]]
    assert given.count("warn") == 1, "并发同一成员只能给一次地板动作（否则冷却形同虚设）"
