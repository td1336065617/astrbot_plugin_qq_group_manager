"""批 A（管理员通知防错）：格式校验 + 运行时失败可见 + 通知测试指令。

对应实现文档 §6 的 U1/U1b/U1c/U1d/U2/U3/U5/U6/U7/U8/U9。
U12（面板提示）由 tools/panel_smoke.py 的策略页必查串 id="notify-warn" 覆盖。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _pkg(name: str):
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.{name}")


main = _pkg("main")
models = _pkg("src.models")
is_valid_umo = models.is_valid_umo
notify_session_state = models.notify_session_state
store_mod = _pkg("src.store")
normalize_settings = store_mod.normalize_settings

GOOD = "爱莉希雅:FriendMessage:FADFF069C2349B38B93EC67E37D101A0"


def run(coro):
    return asyncio.run(coro)


class FakeAudit:
    def __init__(self):
        self.capabilities = []

    async def record_capability(self, **kw):
        self.capabilities.append(kw)


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, *a, **k):
        self.warnings.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))

    def info(self, *a, **k):
        self.infos.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))

    def debug(self, *a, **k):
        pass


class FakeContext:
    def __init__(self, reply=True, raise_exc=None):
        self.calls = []
        self.reply = reply
        self.raise_exc = raise_exc

    async def send_message(self, session, chain):
        self.calls.append(session)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.reply


class FakeStore:
    def __init__(self, settings):
        self._settings = dict(settings)

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


def make_service(session, *, reply=True, raise_exc=None, audit=True):
    svc = object.__new__(main.QQGroupManager)
    svc.store = FakeStore({"notify_session": session})
    svc.context = FakeContext(reply=reply, raise_exc=raise_exc)
    svc.logger = FakeLogger()
    svc.audit = FakeAudit() if audit else None
    return svc


def test_u1_umo_shape_validation():
    assert is_valid_umo(GOOD) is True
    assert is_valid_umo("") is False
    assert is_valid_umo("a:b") is False
    assert is_valid_umo("  " + GOOD + "  ") is True, "两侧空白应 strip 后判"


def test_u1b_message_type_must_be_known():
    assert is_valid_umo("a:bogus:c") is False, "中段必须是 SDK 认识的类型"
    assert is_valid_umo("a:groupmessage:c") is False, "类型大小写敏感"


def test_u1c_session_id_may_contain_colons():
    assert is_valid_umo("a:FriendMessage:x:y") is True, "与 SDK 的 split(chr(34)+chr(58)+chr(34), 2) 同源"


def test_u1d_empty_segment_is_invalid():
    assert is_valid_umo(":FriendMessage:x") is False
    assert is_valid_umo("a:FriendMessage:") is False


def test_u2_normalize_keeps_invalid_value_without_writing_derived_key():
    settings = dict(models.default_settings())
    settings["notify_session"] = "BROKEN"
    out = normalize_settings(settings)
    assert out["notify_session"] == "BROKEN", "非法值不得被静默清空"
    assert "notify_session_valid" not in out, "派生状态不能写进配置（会被持久化到 KV）"


def test_u3_notify_session_state_reports_hint():
    ok = notify_session_state({"notify_session": GOOD})
    bad = notify_session_state({"notify_session": "BROKEN"})
    empty = notify_session_state({})
    assert ok["valid"] is True and ok["hint"].startswith("✅")
    assert bad["valid"] is False and bad["hint"].startswith("⚠️")
    assert empty["configured"] is False and empty["valid"] is False


def test_u5_notify_test_command_reports_success():
    svc = make_service(GOOD)
    out = run(main.QQGroupManager._cmd_notify_test(svc))
    assert any("成功" in line for line in out), out
    assert svc.context.calls == [GOOD]


def test_u6_notify_test_command_refuses_invalid_without_sending():
    svc = make_service("BROKEN")
    out = run(main.QQGroupManager._cmd_notify_test(svc))
    assert any("不合法" in line for line in out), out
    assert svc.context.calls == [], "格式不合法不得盲调 send_message"


def test_u7_notify_test_command_surfaces_send_error():
    svc = make_service(GOOD, raise_exc=ValueError("boom"))
    out = run(main.QQGroupManager._cmd_notify_test(svc))
    assert any("发送失败" in line and "ValueError" in line for line in out), out


def test_u8_runtime_notify_invalid_session_is_recorded_and_not_sent():
    svc = make_service("BROKEN")
    ok, detail = run(main.QQGroupManager._notify(svc, "moderation", {"group_id": "g1"}))
    assert ok is False and "不合法" in detail
    assert svc.context.calls == []
    caps = [c for c in svc.audit.capabilities if c.get("capability") == "notify"]
    assert caps and caps[0].get("err_code") == "invalid_session", caps
    assert svc.logger.warnings, "必须留下明确 warning"


def test_u9_runtime_notify_send_failure_is_recorded():
    svc = make_service(GOOD, reply=False)
    ok, _detail = run(main.QQGroupManager._notify(svc, "moderation", {"group_id": "g1"}))
    assert ok is False
    caps = [c for c in svc.audit.capabilities if c.get("capability") == "notify"]
    assert caps and caps[0].get("err_code") == "send_failed", caps
    assert svc.logger.infos, "仍要保留日志回退"


def test_u9b_missing_audit_attribute_does_not_break_notify():
    svc = make_service("BROKEN", audit=False)
    del svc.audit
    ok, _detail = run(main.QQGroupManager._notify(svc, "moderation", {"group_id": "g1"}))
    assert ok is False, "没有 audit 属性也不能抛异常"

