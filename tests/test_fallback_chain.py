"""W1：备用模型链、异常分类与设置键归一化测试。"""

from __future__ import annotations

import asyncio

from src.moderator import choose_provider_chain, classify_failure, should_fallback
from src.store import normalize_provider_chain, normalize_settings

_KNOWN = ["a/m1", "b/m2", "c/m3", "s/default"]


def test_chain_follows_priority_and_dedupes():
    chain = choose_provider_chain("a/m1", "s/default", ["b/m2", "a/m1", "c/m3"], _KNOWN)
    assert chain == ["a/m1", "b/m2", "c/m3", "s/default"]


def test_chain_skips_unavailable_provider():
    chain = choose_provider_chain("gone/x", "s/default", ["b/m2"], ["b/m2", "s/default"])
    assert chain == ["s/default", "b/m2"]


def test_chain_default_off_equals_old_behaviour():
    assert choose_provider_chain("a/m1", "s/default", [], ["a/m1"]) == ["a/m1"]
    assert choose_provider_chain("", "s/default", None, None) == ["s/default"]


class _Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


def test_classify_failure_matrix():
    assert classify_failure(asyncio.TimeoutError()) == "timeout"
    assert classify_failure(_Err("boom", 429)) == "rate_limited"
    assert classify_failure(_Err("rate limit exceeded")) == "rate_limited"
    assert classify_failure(_Err("x", 503)) == "server_error"
    assert classify_failure(_Err("bad gateway")) == "server_error"
    assert classify_failure(_Err("connection reset")) == "network"
    assert classify_failure(ConnectionError("reset")) == "network"
    assert classify_failure(_Err("blocked by safety policy", 400)) == "safety_refused"
    assert classify_failure(_Err("参数错误", 400)) == "client_error"
    assert classify_failure(_Err("mystery")) == "refuse_unknown"
    assert classify_failure(None) == "refuse_unknown"


def test_should_fallback_only_for_transient_codes():
    for code in ("timeout", "rate_limited", "server_error", "network"):
        assert should_fallback(code)
    for code in ("safety_refused", "client_error", "refuse_unknown", ""):
        assert not should_fallback(code)


def test_normalize_provider_chain_rules():
    assert normalize_provider_chain(["a", " a ", "", "b", 3, "a"]) == ["a", "b"]
    assert normalize_provider_chain("脏数据", ["x"]) == ["x"]
    assert len(normalize_provider_chain([f"p{i}" for i in range(20)])) == 8


def test_default_settings_have_empty_fallback_lists():
    settings = normalize_settings({})
    assert settings["llm_fallback_provider_ids"] == []
    assert settings["join_llm_fallback_provider_ids"] == []
    dirty = normalize_settings({"llm_fallback_provider_ids": ["a", "a", "", 5], "join_llm_fallback_provider_ids": "x"})
    assert dirty["llm_fallback_provider_ids"] == ["a"]
    assert dirty["join_llm_fallback_provider_ids"] == []
