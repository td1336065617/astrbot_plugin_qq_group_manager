"""B1（R3-1）：解析失败要能切备用模型，且拒绝文本不得借备用模型绕过安全策略。"""

from __future__ import annotations

import asyncio

from src.moderator import LLMModerator
from tests.test_join_follow_gate import _plugin_main


def run(coro):
    return asyncio.run(coro)


BAD = "这条消息没什么问题，我直接说结论：不需要处理。"
BAD2 = "看起来只是日常聊天，不用管。"
GOOD = '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,"reason":"x","analysis":"y"}'
REFUSAL = "I cannot help with this request: it violates our safety policy."


class _Provider:
    def __init__(self, pid):
        self.provider_config = {"modalities": ["text", "image"]}
        self._meta = type("M", (), {"id": pid, "model": pid, "type": "openai_chat_completion"})()

    def meta(self):
        return self._meta


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _Ctx:
    """按 provider 返回预设文本；used 记录真实调用顺序（谁被调用、调了几次）。"""

    def __init__(self, ids, texts):
        self.ids = list(ids)
        self.texts = dict(texts)
        self.used = []

    def get_all_providers(self):
        return [_Provider(pid) for pid in self.ids]

    async def get_current_chat_provider_id(self, umo=None):
        return "SESSION"

    async def llm_generate(self, *, chat_provider_id, **kwargs):
        self.used.append(chat_provider_id)
        return _Resp(self.texts.get(chat_provider_id, GOOD))

    async def get_using_provider_async(self, umo=None):  # pragma: no cover - 走到这里就说明漏进了 legacy
        raise AssertionError("不得退化到 legacy 单 provider 调用")


class _Store:
    def __init__(self, settings):
        self._settings = dict(settings)

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def settings(self):
        return dict(self._settings)


class _Audit:
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


BASE = {
    "llm_timeout": 5,
    "sample_rate": 1.0,
    "cache_ttl": 0,
    "llm_min_confidence": 0.7,
    "llm_daily_budget": 0,
}


def _service(settings, ids, texts):
    plugin_main = _plugin_main()
    service = object.__new__(plugin_main.QQGroupManager)
    merged = dict(BASE)
    merged.update(settings)
    service.store = _Store(merged)
    service.context = _Ctx(ids, texts)
    service.logger = _Log()
    service._last_provider_id = ""
    service._last_join_provider_id = ""
    service.audit = _Audit()
    service.moderator = LLMModerator(
        service._llm_call, settings_getter=service.store.settings, logger=service.logger
    )
    return service


def _judge(service):
    plugin_main = _plugin_main()
    request = plugin_main.ModerationRequest(group_id="g1", text="x", umo="")
    return run(service.moderator.judge(request)), request


def test_parse_failure_falls_back_to_next_provider():
    service = _service({"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]}, ["A", "B"], {"A": BAD, "B": GOOD})
    verdict, request = _judge(service)
    assert service.context.used == ["A", "B"], "首跳不可解析必须交给备用模型"
    assert verdict.parse_error is False
    assert verdict.stage == "judged"
    assert verdict.verdict == "allow"
    assert request.fallback_index == 1


def test_all_hops_unparseable_lands_in_parse_error():
    service = _service(
        {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]}, ["A", "B"], {"A": BAD, "B": BAD2}
    )
    verdict, _ = _judge(service)
    assert service.context.used == ["A", "B"]
    assert verdict.stage == "parse_error", "全跳不可解析必须落 parse_error，而不是 api_error"
    assert verdict.parse_error is True
    assert verdict.raw, "原始输出必须留档"


def test_text_level_refusal_is_not_retried_on_fallback():
    service = _service(
        {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]}, ["A", "B"], {"A": REFUSAL, "B": GOOD}
    )
    verdict, _ = _judge(service)
    assert service.context.used == ["A"], "文本级安全拒绝不得换模型重试（否则等于绕过策略）"
    assert verdict.stage == "parse_error"


def test_without_fallbacks_behaviour_is_unchanged():
    service = _service({"llm_provider_id": "A", "llm_fallback_provider_ids": []}, ["A", "B"], {"A": BAD})
    verdict, request = _judge(service)
    assert service.context.used == ["A"]
    assert request.fallback_index == 0
    assert verdict.stage == "parse_error"


def test_unparseable_hop_leaves_capability_row():
    service = _service({"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]}, ["A", "B"], {"A": BAD, "B": GOOD})
    _judge(service)
    hops = [row for row in service.audit.rows if row.get("capability") == "llm_hop"]
    assert hops, "每一跳的解析失败都要留下可定位的记录"
    assert hops[0].get("err_code") == "parse_error"
    assert "sha256" in hops[0].get("note", ""), "只存摘要，不存正文"
    assert hops[0].get("note", "").startswith("A|"), "摘要要带上是哪个 provider 的失败"
    assert not any("不需要处理" in str(row.get("note", "")) for row in service.audit.rows)


#: 只漏一个开引号、可被 A1 保守修补救回的输出
REPAIRABLE = (
    "{\n  \"analysis\": 只是发了个表情,\",\n  \"verdict\": \"allow\",\n  \"category\": \"无\",\n"
    "  \"severity\": 1,\n  \"confidence\": 0.98,\n  \"reason\": \"无违规内容\",\n  \"suggested_action\": \"none\"\n}"
)


def test_repairable_first_hop_is_not_discarded_when_fallbacks_exist():
    """A1×B1 组合回归：配了备用模型时，可被修补救回的首跳判定不得被丢弃。"""
    service = _service({"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]}, ["A", "B"], {"A": REPAIRABLE, "B": BAD})
    verdict, request = _judge(service)
    assert service.context.used == ["A"], "首跳可修补就应该直接采用，不该再问备用模型"
    assert verdict.stage == "judged"
    assert verdict.verdict == "allow"
    assert request.fallback_index == 0


def test_reasoning_text_mentioning_risk_is_not_a_refusal():
    """讲道理的长文本里出现 risk/policy 不该被当成安全拒绝（旧实现会误判，B1 随之失效）。"""
    service = _service(
        {"llm_provider_id": "A", "llm_fallback_provider_ids": ["B"]},
        ["A", "B"],
        {"A": "The risk is low and the policy allows it. No action needed.", "B": GOOD},
    )
    verdict, request = _judge(service)
    assert service.context.used == ["A", "B"], "不是拒绝 => 应当继续切备用模型"
    assert verdict.stage == "judged"
    assert request.fallback_index == 1


def test_rule_source_is_not_capped_by_low_confidence():
    """低置信封顶只针对 source=llm；规则/刷屏判定不受影响。"""
    from src.models import Verdict

    settings = {"action_matrix": {"violation": {"3": ["warn", "mute"]}}}
    from src.actions import ActionExecutor

    ex = ActionExecutor(api=None, store=None)
    rule = Verdict(verdict="violation", category="广告引流", severity=3, confidence=0.1, stage="judged", source="rule")
    assert ex.plan_actions(verdict=rule, settings=settings) == ["warn", "mute"]

