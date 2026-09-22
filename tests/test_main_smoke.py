"""插件契约冒烟测试：metadata、main 导入、指令匹配、Web 路由。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _import_main():
    parent = str(PLUGIN_ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


def test_metadata_contract():
    yaml = importlib.import_module("yaml")
    meta = yaml.safe_load((PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["name"] == PLUGIN_ROOT.name
    assert meta["support_platforms"] == [
        "qq_official",
        "qq_official_webhook",
        "aiocqhttp",
    ]
    assert meta["version"]
    assert meta["author"]
    assert meta["desc"]


def test_main_imports_and_commands():
    main = _import_main()
    assert PLUGIN_ROOT.name == main.PLUGIN_NAME
    assert main.VERSION
    commands = importlib.import_module(f"{PLUGIN_ROOT.name}.src.commands")
    match = commands.match_command
    assert match("群信息") == ("群信息", [])
    assert match("禁言 @张三 10分钟") == ("禁言", ["@张三", "10分钟"])
    assert match("审核状态")[0] == "审核状态"
    assert match("随便聊聊") == ("", [])
    assert match("") == ("", [])
    assert not set(commands.PUBLIC_COMMANDS) & set(commands.ADMIN_ONLY_COMMANDS)
    assert not set(commands.PUBLIC_COMMANDS) & set(commands.GROUP_ADMIN_COMMANDS)


class FakeContext:
    """只记录 register_web_api 调用的最小上下文。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append({"route": route, "methods": methods, "desc": desc, "handler": handler})


def test_web_api_registers_prefixed_routes():
    web_api = importlib.import_module(f"{PLUGIN_ROOT.name}.src.web_api")

    class Service:
        context = FakeContext()

    service = Service()
    web_api.WebApi(service).register()
    routes = service.context.routes
    assert routes, "至少应注册一个 Web API"
    prefix = "/" + PLUGIN_ROOT.name + "/"
    assert all(item["route"].startswith(prefix) for item in routes)
    paths = {item["route"] for item in routes}
    for expected in (
        prefix + "config",
        prefix + "summary",
        prefix + "groups",
        prefix + "groups/probe",
        prefix + "groups/moderation",
        prefix + "logs/events",
        prefix + "logs/api",
        prefix + "db/info",
        prefix + "events/stream",
        prefix + "selfcheck",
        prefix + "instructions",
        prefix + "dryrun",
        prefix + "rules/test",
        prefix + "mutes",
        prefix + "mutes/unmute",
        prefix + "members/search",
        prefix + "blacklist",
        prefix + "joins",
        prefix + "joins/fetch",
        prefix + "joins/decide",
        prefix + "policy",
    ):
        assert expected in paths, expected
    for kind in ("events", "actions", "api", "capability"):
        assert prefix + "logs/" + kind in paths


def test_event_bus_pub_sub():
    web_api = importlib.import_module(f"{PLUGIN_ROOT.name}.src.web_api")
    bus = web_api.EventBus()
    queue = bus.subscribe("audit")
    bus.publish("audit", {"kind": "events"})
    assert queue.get_nowait()["kind"] == "events"
    bus.unsubscribe("audit", queue)
    assert bus.subscriber_count("audit") == 0
