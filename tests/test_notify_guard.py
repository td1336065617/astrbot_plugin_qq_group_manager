"""批 A（管理员通知防错）：格式校验 + 运行时失败可见 + 通知自检（面板入口）。

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

    def record_capability(self, **kw):  # 与 src/audit.py 的真实签名一致（同步）
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
    out = run(main.QQGroupManager.notify_test_once(svc))
    assert any(("送出" in line or "成功" in line) for line in out), out
    assert svc.context.calls == [GOOD]


def test_u6_notify_test_command_refuses_invalid_without_sending():
    svc = make_service("BROKEN")
    out = run(main.QQGroupManager.notify_test_once(svc))
    assert any("不合法" in line for line in out), out
    assert svc.context.calls == [], "格式不合法不得盲调 send_message"


def test_u7_notify_test_command_surfaces_send_error():
    svc = make_service(GOOD, raise_exc=ValueError("boom"))
    out = run(main.QQGroupManager.notify_test_once(svc))
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


class FakePlatform:
    def __init__(self, pid, name=""):
        self._meta = type("M", (), {"id": pid, "name": name or pid})()

    def meta(self):
        return self._meta


class FakeConversation:
    def __init__(self, umo):
        self.user_id = umo


class FakeDB:
    def __init__(self, umos):
        self.umos = list(umos)
        self.calls = []

    async def get_all_conversations(self, page=1, page_size=20):
        self.calls.append((page, page_size))
        return [FakeConversation(u) for u in self.umos]


def make_ctx_with_sessions(umos, platform_ids=("爱莉希雅",), with_db=True):
    class Ctx:
        pass

    ctx = Ctx()
    if with_db:
        ctx.get_db = lambda: FakeDB(umos)
    ctx.platform_manager = type("PM", (), {"platform_insts": [FakePlatform(p) for p in platform_ids]})()
    return ctx


def test_u3_sessions_candidates_merges_sources_and_filters_invalid():
    svc = make_service(GOOD)
    svc.context = make_ctx_with_sessions([GOOD, "a:FriendMessage:x:y", "BROKEN", "a:b:c"])
    svc._seen_sessions = ["爱莉希雅:GroupMessage:G1", GOOD]
    data = run(main.QQGroupManager.sessions_candidates(svc))
    umos = [item["umo"] for item in data["items"]]
    assert GOOD in umos and "a:FriendMessage:x:y" in umos
    assert "BROKEN" not in umos and "a:b:c" not in umos, "非法会话不得进候选"
    assert len(umos) == len(set(umos)), "候选要去重"
    assert data["platform_ids"] == ["爱莉希雅"]
    assert all(item["label"] for item in data["items"]), "每项都要有展示名"


def test_u4_sessions_candidates_tolerates_missing_db():
    svc = make_service(GOOD)
    svc.context = make_ctx_with_sessions([], with_db=False)
    svc._seen_sessions = []
    data = run(main.QQGroupManager.sessions_candidates(svc))
    assert data["items"] == []
    assert data["platform_ids"] == ["爱莉希雅"]


def test_u10_webhook_not_configured_makes_no_request():
    svc = make_service(GOOD)
    svc.store = FakeStore({"notify_session": GOOD, "notify_webhook_url": ""})
    calls = []
    svc.poster = lambda url, body, timeout: calls.append((url, body, timeout)) or (True, "HTTP 200")
    ok, _detail = run(main.QQGroupManager._notify(svc, "moderation", {"group_id": "g1"}))
    assert ok is True
    assert calls == [], "未配置 webhook 不得发起任何请求"


def test_u11_webhook_delivers_when_session_fails():
    import json as _json

    svc = make_service(GOOD, reply=False)
    svc.store = FakeStore({
        "notify_session": GOOD,
        "notify_webhook_url": "https://example.invalid/hook",
        "notify_webhook_timeout": 3,
    })
    seen = {}

    def poster(url, body, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        seen["body"] = _json.loads(body.decode("utf-8"))
        return True, "HTTP 200"

    svc.poster = poster
    ok, detail = run(main.QQGroupManager._notify(svc, "moderation", {"group_id": "g1", "reason": "测试"}))
    assert ok is True and "webhook" in detail
    assert seen["url"] == "https://example.invalid/hook" and seen["timeout"] == 3.0
    assert set(seen["body"]) == {"kind", "text", "payload"}
    assert seen["body"]["kind"] == "moderation" and "内容审核告警" in seen["body"]["text"]
    assert seen["body"]["payload"]["reason"] == "测试"


def test_u11b_webhook_failure_is_recorded_not_raised():
    svc = make_service(GOOD, reply=False)
    svc.store = FakeStore({"notify_session": GOOD, "notify_webhook_url": "https://example.invalid/hook"})
    svc.poster = lambda url, body, timeout: (False, "HTTP 500")
    ok, _detail = run(main.QQGroupManager._notify(svc, "moderation", {"group_id": "g1"}))
    assert ok is False
    codes = [c.get("err_code") for c in svc.audit.capabilities]
    assert "send_failed" in codes and "webhook_failed" in codes, codes


def test_u12b_notify_test_once_supports_webhook_only_setup():
    svc = make_service("")
    svc.store = FakeStore({"notify_session": "", "notify_webhook_url": "https://example.invalid/hook"})
    svc.poster = lambda url, body, timeout: (True, "HTTP 200")
    out = run(main.QQGroupManager.notify_test_once(svc))
    assert any("送出" in line or "成功" in line for line in out), out


def test_u13_webhook_post_uses_timeout_keyword_and_bytes_body(monkeypatch):
    """P1 回归点：urlopen 的第二个位置参数是 data，timeout 必须用关键字传。"""
    import urllib.request

    seen = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, *args, **kwargs):
        seen["request"] = request
        seen["args"] = args
        seen["kwargs"] = kwargs
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, detail = main._webhook_post("https://example.invalid/hook", b'{"a": 1}', 7)
    assert ok is True and "HTTP 200" in detail
    assert seen["args"] == (), "不得把 timeout 当位置参数传（那是 data）"
    assert seen["kwargs"].get("timeout") == 7.0
    assert isinstance(seen["request"].data, bytes)


def test_u14_webhook_real_transport_reaches_local_http_server():
    """端到端：真起一个本地 HTTP server，用真 _webhook_post 发一次。"""
    import http.server
    import json as _json
    import threading

    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received["body"] = _json.loads(self.rfile.read(length).decode("utf-8"))
            received["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook"
    try:
        ok, detail = main._webhook_post(url, b'{"hello": "world"}', 5)
        thread.join(timeout=5)
    finally:
        server.server_close()
    assert ok is True, detail
    assert received.get("body") == {"hello": "world"}, "服务端必须真的收到 JSON 体"
    assert "json" in (received.get("ctype") or "")


def test_u15_sessions_candidates_works_before_any_message_seen():
    """P2 回归点：重启后还没收到任何消息时，候选接口不得抛 AttributeError。"""
    svc = make_service(GOOD)
    svc.context = make_ctx_with_sessions([GOOD])
    if hasattr(svc, "_seen_sessions"):
        del svc._seen_sessions
    data = run(main.QQGroupManager.sessions_candidates(svc))
    assert [item["umo"] for item in data["items"]] == [GOOD]


def test_u16_notify_test_once_tries_webhook_when_session_invalid():
    svc = make_service("BROKEN")
    svc.store = FakeStore({
        "notify_session": "BROKEN",
        "notify_webhook_url": "https://example.invalid/hook",
    })
    svc.poster = lambda url, body, timeout: (True, "HTTP 200")
    out = run(main.QQGroupManager.notify_test_once(svc))
    assert any("送出" in line or "成功" in line for line in out), out


def test_u17_session_label_keeps_colons_in_session_id():

    mod = _pkg("src.notify_sessions")
    label = mod.session_label("a:FriendMessage:x:y")
    assert "x:y" in label, label


def test_u18_policy_view_refreshes_sessions_on_open():
    """契约：进入策略页必须重新拉候选（否则插件重启后候选会一直过期）。

    这条是对「自动刷新」唯一有效的守卫——渲染冒烟测不到它（stub 永远返回候选）。
    """
    source = (PLUGIN_ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    assert "async function refreshSessions(force)" in source, "refreshSessions 助手不见了"
    start = source.index("async function viewPolicy(root) {")
    body = source[start:source.index("\n}", start)]
    # 只认开头几行：「刷新候选」按钮的 onclick 里也含同一句，不能作为通过依据
    head_lines = body.splitlines()[:4]
    assert any("await refreshSessions(true);" in line for line in head_lines), (
        "viewPolicy 打开时必须强制刷新候选（应在函数开头 4 行内）"
    )
    load_start = source.index("async function loadConfig(force) {")
    load_body = source[load_start:source.index("\n}", load_start)]
    assert "sessions/list" not in load_body, "候选不应再在 loadConfig 里一次性拉取（会过期）"
