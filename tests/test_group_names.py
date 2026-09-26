"""群名补全测试：只补没名字的群；官方/OneBot 统一走 channel_for().get_group_info()。"""
from __future__ import annotations

import asyncio
import types

from src.group_names import refresh_group_names
from src.models import GroupConfig, GroupProfile


class FakeChannel:
    def __init__(self, name="", error=None, profile=None):
        self.name = name
        self.error = error
        self.profile = profile
        self.calls = []

    async def get_group_info(self, group_id, *, caller="probe"):
        self.calls.append((group_id, caller))
        if self.error is not None:
            raise self.error
        if self.profile is not None:
            return self.profile
        return GroupProfile(group_openid=group_id, name=self.name)


class FakeRouter:
    def __init__(self, channels):
        self.channels = channels
        self.asked = []

    def channel_for(self, platform_id):
        self.asked.append(platform_id)
        return self.channels.get(platform_id, FakeChannel())


class FakeStore:
    def __init__(self, groups):
        self._groups = {item.group_id: item for item in groups}
        self.flushed = 0

    def groups(self):
        return self._groups

    async def ensure_group(self, group_id, *, name="", **kwargs):
        config = self._groups[group_id]
        if name:
            config.name = name
        return config

    async def flush(self):
        self.flushed += 1


def _group(gid, pid, name=""):
    return GroupConfig(group_id=gid, platform_id=pid, name=name)


def test_refresh_only_touches_groups_without_name():
    official = FakeChannel(name="官方测试群")
    onebot = FakeChannel(name="不该被调用")
    store = FakeStore([_group("C8D6", "qq_official"), _group("100", "aiocqhttp", "已有名字")])
    router = FakeRouter({"qq_official": official, "aiocqhttp": onebot})

    result = asyncio.run(refresh_group_names(store, router))

    assert result == {"updated": 1, "failed": 0, "skipped": 0, "pending": 1}
    assert official.calls == [("C8D6", "webui")]
    assert onebot.calls == []
    assert router.asked == ["qq_official"]
    assert store.groups()["C8D6"].name == "官方测试群"
    assert store.flushed == 1


def test_refresh_noop_when_every_group_has_name():
    store = FakeStore([_group("100", "aiocqhttp", "集训群")])
    router = FakeRouter({"aiocqhttp": FakeChannel(name="x")})
    result = asyncio.run(refresh_group_names(store, router))
    assert result == {"updated": 0, "failed": 0, "skipped": 0, "pending": 0}
    assert store.flushed == 0


def test_refresh_counts_failures_and_continues():
    ok = FakeChannel(name="官方群")
    broken = FakeChannel(error=RuntimeError("boom"))
    empty = FakeChannel(name="")
    warnings = []
    store = FakeStore([
        _group("bad", "p_bad"),
        _group("empty", "p_empty"),
        _group("good", "p_ok"),
    ])
    router = FakeRouter({"p_bad": broken, "p_empty": empty, "p_ok": ok})

    result = asyncio.run(
        refresh_group_names(store, router, on_warn=lambda gid, exc: warnings.append((gid, str(exc))))
    )

    assert result["updated"] == 1 and result["failed"] == 2 and result["pending"] == 3
    assert warnings and warnings[0][0] == "bad"
    assert store.groups()["good"].name == "官方群"


def test_refresh_respects_limit():
    store = FakeStore([_group(f"g{i}", "p") for i in range(3)])
    router = FakeRouter({"p": FakeChannel(name="群")})
    result = asyncio.run(refresh_group_names(store, router, limit=2))
    assert result["updated"] == 2 and result["skipped"] == 1 and result["pending"] == 3


def test_refresh_names_route_registered():
    from src import web_api

    class FakeContext:
        def __init__(self):
            self.routes = []

        def register_web_api(self, route, handler, methods, desc):
            self.routes.append({"route": route, "methods": methods, "desc": desc})

    service = types.SimpleNamespace(context=FakeContext())
    web_api.WebApi(service).register()
    paths = {item["route"] for item in service.context.routes}
    assert any(path.endswith("/groups/refresh-names") for path in paths), sorted(paths)[:5]
