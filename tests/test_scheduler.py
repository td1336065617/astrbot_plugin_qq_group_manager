"""/S-8 护栏：调度器 run_immediately 语义，以及入群积压清理任务的接线。

背景：TaskSpec.run_immediately 默认 False，任务会**先睡一整轮**再首次执行。
join_pending_sweep 的间隔是 86400s，若沿用默认值，在「插件比一天更频繁重启」的环境里
（今天上线当天就重启了两次）这个清理任务可能**永远不执行** —— 功能看起来交付了，实际是死的。
"""

from __future__ import annotations

import asyncio

from src.scheduler import TaskScheduler, TaskSpec
from tests.test_join_follow_gate import _plugin_main


def run(coro):
    return asyncio.run(coro)


class _Store:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


def test_scheduler_runs_immediately_only_when_flagged():
    "run_immediately=True 的任务必须不等间隔就执行一次；未标记的必须先等一轮。"
    calls: list[str] = []

    async def factory():
        calls.append("run")

    async def park(_seconds):
        await asyncio.sleep(3600)  # 把循环停住，避免忙转

    async def scenario():
        scheduler = TaskScheduler(logger=None, clock=lambda: 0.0, sleep=park)
        scheduler.add(TaskSpec("now", factory, 100.0, run_immediately=True))
        scheduler.add(TaskSpec("later", factory, 100.0))
        await scheduler.start()
        await asyncio.sleep(0.05)  # 让事件循环真正调度一次
        await scheduler.stop()

    run(scenario())
    assert calls == ["run"], f"只应有 run_immediately 的任务执行一次，实际 {calls}"


def test_join_pending_sweep_is_registered_run_immediately():
    "接线断言：每日清理任务必须 run_immediately=True，否则可能永远不跑。"
    plugin_main = _plugin_main()
    service = object.__new__(plugin_main.QQGroupManager)
    service.scheduler = TaskScheduler(logger=None)
    service.store = _Store({"join_poll_interval": "60"})
    service._register_tasks()
    spec = service.scheduler._specs["join_pending_sweep"]
    assert spec.run_immediately is True
    assert spec.interval == 86400


def test_other_tasks_keep_default_first_run():
    "其余任务间隔都不超过半小时，保持默认（不抢启动时的资源）—— 这条锁住「只改该改的那一个」。"
    plugin_main = _plugin_main()
    service = object.__new__(plugin_main.QQGroupManager)
    service.scheduler = TaskScheduler(logger=None)
    service.store = _Store({"join_poll_interval": "60"})
    service._register_tasks()
    flagged = [name for name, spec in service.scheduler._specs.items() if spec.run_immediately]
    assert flagged == ["join_pending_sweep"], f"只有清理任务应立即执行，实际 {flagged}"

