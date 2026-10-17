"""批 1（#1 纯图盲判 / #9 头像复核）：能力过滤 + 不计失败的降级路径。

覆盖实现文档 §1.5 的 T1、T1b、T2、T3、T4、T5、T6、T7、T8b。
注意：所有插件模块都通过插件包导入（与 main.py 的 .src.* 是同一份），否则异常类会出现两份。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _pkg_module(name: str):
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.{name}")


main = _pkg_module('main')
mod_mod = _pkg_module('src.moderator')
join_mod = _pkg_module('src.join_review')
QQGroupManager = main.QQGroupManager
LLMModerator = mod_mod.LLMModerator
ModerationRequest = mod_mod.ModerationRequest
NoImageCapableProvider = mod_mod.NoImageCapableProvider
JoinReviewer = join_mod.JoinReviewer

OK_JSON = '{"verdict": "allow", "category": "无", "confidence": 0.9, "reason": "ok"}'


def run(coro):
    return asyncio.run(coro)


class FakeAudit:
    def __init__(self):
        self.capabilities = []
        self.actions = []

    def record_capability(self, **kw):
        self.capabilities.append(kw)

    def record_action(self, **kw):
        self.actions.append(kw)


class FakeProvider:
    def __init__(self, pid, modalities=None):
        self._pid = pid
        self.provider_config = {} if modalities is None else {'modalities': list(modalities)}

    def meta(self):
        outer = self

        class M:
            id = outer._pid
        return M()


class FakeContext:
    def __init__(self, providers, reply=OK_JSON):
        self._providers = providers
        self._reply = reply
        self.calls = []

    def get_all_providers(self):
        return self._providers

    async def get_current_chat_provider_id(self, umo=None):
        return ''

    async def llm_generate(self, chat_provider_id='', prompt='', system_prompt='', contexts=None, image_urls=None):
        self.calls.append(chat_provider_id)
        if isinstance(self._reply, Exception):
            raise self._reply
        reply = self._reply

        class R:
            completion_text = reply
        return R()


class FakeStore:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def settings(self):
        return dict(self._settings)

    def effective_join_settings(self, group_id):
        return dict(self._settings)


class FakeLogger:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class FakeModerator:
    def provider_circuit_open(self, provider_id):
        return False

    def note_provider_result(self, provider_id, ok, code=''):
        pass


def make_manager(providers, *, fallbacks=('img/b',), primary='img/a', audit='auto', circuit=False, reply=OK_JSON):
    mgr = object.__new__(QQGroupManager)
    mgr.context = FakeContext(providers, reply=reply)
    mgr.store = FakeStore({
        'llm_provider_id': primary,
        'llm_fallback_provider_ids': list(fallbacks),
        'llm_timeout': 5,
        'notify_session': '',
    })
    mgr.logger = FakeLogger()
    if audit != 'missing':
        mgr.audit = FakeAudit() if audit == 'auto' else None
    mod = FakeModerator()
    if circuit:
        mod.provider_circuit_open = lambda pid: True
    mgr.moderator = mod
    mgr._note_used_provider = lambda setting, pid: None
    return mgr


def req(text='', images=('/tmp/a.jpg',)):
    return ModerationRequest(group_id='g1', text=text, image_urls=list(images))


def test_t1_pure_image_all_candidates_text_only_raises_and_logs():
    mgr = make_manager([FakeProvider('img/a', ['text']), FakeProvider('img/b', ['text'])])
    with pytest.raises(NoImageCapableProvider):
        run(mgr._llm_call(req(), 'sys', 'user'))
    assert mgr.context.calls == [], '不应调用任何不支持图片的模型'
    caps = [c for c in mgr.audit.capabilities if c.get('capability') == 'image_filter']
    assert caps and caps[0].get('err_code') == 'no_image_provider'


def test_t2_unknown_modalities_still_called():
    mgr = make_manager([FakeProvider('img/a'), FakeProvider('img/b')])
    assert run(mgr._llm_call(req(), 'sys', 'user')) == OK_JSON
    assert mgr.context.calls == ['img/a'], '能力未知按支持处理，应照常调用'


def test_t3_text_message_chain_unchanged():
    mgr = make_manager([FakeProvider('img/a', ['text']), FakeProvider('img/b', ['text'])], reply='text-out')
    run(mgr._llm_call(req(text='正文非空'), 'sys', 'user'))
    assert mgr.context.calls == ['img/a'], '图文混排不应因能力过滤改变候选顺序'


def test_t4_primary_available_chain_unchanged():
    mgr = make_manager([FakeProvider('img/a', ['image', 'text']), FakeProvider('img/b', ['text'])], reply='out')
    run(mgr._llm_call(req(), 'sys', 'user'))
    assert mgr.context.calls == ['img/a']


def test_t6_circuit_filter_keeps_first_candidate_without_raise():
    mgr = make_manager([FakeProvider('img/a', ['image']), FakeProvider('img/b', ['image'])], circuit=True, reply='out')
    assert run(mgr._llm_call(req(), 'sys', 'user')) == 'out'
    assert mgr.context.calls == ['img/a'], '全熔断时保留首个候选，且不因能力过滤而 raise'


def test_t7_missing_audit_attribute_does_not_break_parse_error_path():
    mgr = make_manager([FakeProvider('img/a', ['text'])], audit='missing', reply='不是 JSON')
    out = run(mgr._llm_call(req(text='正文'), 'sys', 'user', validate=lambda t: False))
    assert out == '不是 JSON'


def make_moderator(response):
    async def provider_call(request, system_prompt, user_prompt, validate=None):
        if isinstance(response, Exception):
            raise response
        return response

    settings = {'llm_timeout': 5, 'sample_rate': 1.0, 'llm_min_confidence': 0.7, 'cache_ttl': 0}
    return LLMModerator(provider_call, settings_getter=lambda: dict(settings), clock=lambda: 0.0)


def test_t1b_content_path_records_api_error_verdict():
    mod = make_moderator(NoImageCapableProvider('无支持图片的候选，未调用模型'))
    verdict = run(mod.judge(ModerationRequest(group_id='g1', text='', image_urls=['/tmp/a.jpg'])))
    assert verdict.stage == 'api_error'
    assert verdict.verdict == 'review'
    assert '无支持图片的候选' in verdict.reason


def test_t8b_capability_filter_does_not_count_as_failure_or_open_circuit():
    mod = make_moderator(NoImageCapableProvider('无支持图片的候选，未调用模型'))
    for _ in range(8):
        run(mod.judge(ModerationRequest(group_id='g1', text='', image_urls=['/tmp/a.jpg'])))
    assert mod.stats.failures == 0, '能力过滤为空不得计入 provider 失败'
    assert mod._consecutive_failures == 0
    assert mod.stats.circuit_open_until == 0, '连续多次也不得熔断'


def test_t5_avatar_review_marks_image_required_and_skips_validate():
    seen = {}

    async def judge_call(system_prompt, user_prompt, **kw):
        seen.update(kw)
        return '{"risk": false, "confidence": 0.9, "reason": "ok"}'

    reviewer = JoinReviewer(api=None, store=FakeStore(), audit=FakeAudit(), judge_call=judge_call)
    settings = {'join_avatar_only_below': 0.95}
    decision = reviewer.parse_decision('{"decision": "approve", "confidence": 0.9, "reason": "ok"}', {}, settings=settings, mode='auto')
    run(reviewer._avatar_review(decision, '/tmp/avatar.jpg', {}, settings))
    assert seen.get('image_required') is True, '头像复核必须声明只能靠图片判断'
    assert 'validate' not in seen, '头像输出没有 decision，不能挂入群判定校验'

