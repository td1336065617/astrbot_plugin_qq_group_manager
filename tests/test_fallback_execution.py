"""W1-4/W1-5/W1-6：回退执行、路由与按 provider 记账的端到端行为。"""

from __future__ import annotations

import asyncio

from src.moderator import LLMModerator
from tests.test_join_follow_gate import _plugin_main


def run(coro):
    return asyncio.run(coro)


class _Provider:
    def __init__(self, pid):
        self.provider_config = {"modalities": ["text", "image"]}
        self._meta = type("M", (), {"id": pid, "model": pid, "type": "openai_chat_completion"})()

    def meta(self):
        return self._meta


class _Resp:
    completion_text = '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,"reason":"x","analysis":"y"}'


class _Ctx:
    """假上下文：fail_for 里的 id 前 fail_times 次调用抛 exc。"""

    def __init__(self, ids, fail_for=None, fail_times=99, exc=None):
        self.ids = list(ids)
        self.used = []
        self._left = {pid: fail_times for pid in (fail_for or [])}
        self._exc = exc or TimeoutError("boom")

    def get_all_providers(self):
        return [_Provider(pid) for pid in self.ids]

    async def get_current_chat_provider_id(self, umo=None):
        return "SESSION"

    async def llm_generate(self, *, chat_provider_id, **kwargs):
        self.used.append(chat_provider_id)
        if self._left.get(chat_provider_id, 0) > 0:
            self._left[chat_provider_id] -= 1
            raise self._exc
        return _Resp()

    async def get_using_provider_async(self, umo=None):
        return None


class _Store:
    def __init__(self, settings):
        self._settings = dict(settings)

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def settings(self):
        return dict(self._settings)


class _Audit:
    """只记录 capability 写入。"""

    def __init__(self):
        self.rows = []

    def record_capability(self, **kwargs):
        self.rows.append(kwargs)
        return True


class _Log:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def _service(settings, ids, **ctx_kwargs):
    plugin_main = _plugin_main()
    service = object.__new__(plugin_main.QQGroupManager)
    service.store = _Store(settings)
    service.context = _Ctx(ids, **ctx_kwargs)
    service.logger = _Log()
    service._last_provider_id = ""
    service._last_join_provider_id = ""
    service.audit = _Audit()
    service.moderator = LLMModerator(
        service._llm_call, settings_getter=service.store.settings, logger=service.logger
    )
    return service


def _call(service):
    plugin_main = _plugin_main()
    request = plugin_main.ModerationRequest(group_id="g1", text="x", umo="")
    text = run(plugin_main.QQGroupManager._llm_call(service, request, "系统", "用户"))
    return request, text


def test_falls_back_to_next_provider_on_timeout():
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"], "llm_timeout": 1}
    service = _service(settings, ["A", "B", "SESSION"], fail_for=["A"])
    request, text = _call(service)
    assert text.startswith("{")
    assert service.context.used[:3] == ["A", "A", "B"], "超时先重试同一 provider 一次，再切备用"
    assert request.fallback_index == 1


def test_falls_back_on_rate_limit_and_records_provider_state():
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"], "llm_timeout": 1}
    exc = RuntimeError("429 too many requests")
    service = _service(settings, ["A", "B", "SESSION"], fail_for=["A"], exc=exc)
    request, _ = _call(service)
    assert request.fallback_index == 1
    assert service.moderator.provider_circuit_open("A") is False, "429 不是超时：不重试，直接切"


def test_safety_refusal_does_not_fall_back():
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"], "llm_timeout": 1}
    exc = RuntimeError("blocked by safety policy")
    service = _service(settings, ["A", "B", "SESSION"], fail_for=["A"], exc=exc)
    try:
        _call(service)
    except RuntimeError:
        pass
    else:
        raise AssertionError("安全策略拒绝必须直接抛出，不得回退")
    assert service.context.used == ["A"], "安全拒绝不得重试、不得切换"


def test_default_off_keeps_single_provider():
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": [], "llm_timeout": 1}
    service = _service(settings, ["A", "B", "SESSION"])
    request, _ = _call(service)
    assert service.context.used == ["A"]
    assert request.fallback_index == 0


def test_provider_circuit_opens_after_threshold_failures():
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"], "llm_timeout": 1, "circuit_break_threshold": 3}
    service = _service(settings, ["A", "B", "SESSION"])
    for _ in range(3):
        service.moderator.note_provider_result("A", ok=False, code="server_error")
    assert service.moderator.provider_circuit_open("A") is True
    service.moderator.note_provider_result("A", ok=True)
    assert service.moderator.provider_circuit_open("A") is True, "成功只清计数，不清已开的熔断窗口"


def test_failure_is_recorded_with_classified_err_code():
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"], "llm_timeout": 1}
    service = _service(settings, ["A", "B", "SESSION"], fail_for=["A"])
    _call(service)
    codes = [row.get("err_code") for row in service.audit.rows]
    assert codes, "失败必须留下 capability 记录"
    assert set(codes) == {"timeout"}, "失败码必须是分类结果，而不是裸异常名"
    assert all(row.get("capability") == "llm_judge" for row in service.audit.rows)


class ProviderNotFoundError(Exception):  # noqa: N818 - 名字必须与 SDK 一致
    """名字必须与 SDK 一致：utils.classify_failure 按类型名识别。"""


def test_provider_not_found_falls_back_to_next():
    "候选 id 不存在（用户删/改了模型）时必须继续回退，而不是整条链失败。"
    settings = {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]}
    service = _service(settings, ["A", "B"], fail_for=["A"], exc=ProviderNotFoundError("Provider A not found"))
    request, text = _call(service)
    assert text.startswith("{")
    assert service.context.used[0] == "A" and service.context.used[-1] == "B", "应切到备用 B"
    assert request.fallback_index == 1
    codes = {row.get("err_code") for row in service.audit.rows if not row.get("ok")}
    assert codes == {"provider_not_found"}, "失败原因必须归类为 provider_not_found"


class _SafetyRefusal(Exception):
    """模拟平台内容安全拒绝：400 + content_filter 特征词。"""

    status_code = 400


def test_safety_refusal_does_not_trip_provider_circuit():
    "A-1 回归：安全拒绝不是「provider 挂了」。若把它计入熔断，阈值一到主模型被熔断，"
    "后续安全拒绝就被静默回退到备用模型（回到 allow 是安全事故）。"
    settings = {
        "llm_provider_id": "A",
        "llm_fallback_provider_ids": ["B"],
        "circuit_break_threshold": 2,
    }
    service = _service(settings, ["A", "B"], fail_for=["A"], exc=_SafetyRefusal("blocked by content_filter"))
    for _ in range(4):  # 4 次安全拒绝，远超阈值 2
        try:
            _call(service)
        except _SafetyRefusal:
            continue
        raise AssertionError("安全拒绝必须原样抛出，不能变成 allow")
    assert service.moderator.provider_circuit_open("A") is False, "安全拒绝不得触发 provider 熔断"
    assert set(service.context.used) == {"A"}, "安全拒绝不得静默切到备用模型"


def test_timeout_retry_counts_failure_once_per_logical_call():
    "A-2 回归：同 provider 超时重试两次，一次逻辑调用只能记一次失败（否则熔断阈值被加速一倍）。"
    settings = {
        "llm_provider_id": "A",
        "llm_fallback_provider_ids": ["B"],
        "circuit_break_threshold": 5,
    }
    service = _service(settings, ["A", "B"], fail_for=["A"], fail_times=2, exc=TimeoutError("t"))
    _request, text = _call(service)
    assert text.startswith("{")
    assert service.context.used[:3] == ["A", "A", "B"]
    assert service.moderator._provider_failures.get("A") == 1, "一次逻辑调用只记一次失败"
