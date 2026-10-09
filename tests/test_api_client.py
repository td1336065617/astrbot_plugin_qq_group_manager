"""QQGroupAPI 单元测试：限频、重试、错误码映射、dry-run、能力探测。"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.api_client import (
    SEM_NOT_ADMIN,
    SEM_NOT_WHITELISTED,
    SEM_TIMEOUT,
    QQApiError,
    QQGroupAPI,
    TokenBucket,
    describe_error,
)
from src.models import (
    CAP_GROUP_INFO,
    CAP_IS_ADMIN,
    CAP_MEMBER_LIST,
    CAP_MUTE,
    CAP_REMOVE_MEMBER,
    ERR_NOT_ADMIN,
    ERR_NOT_WHITELISTED,
)
from tests.fakes import FakeTransport, RaisingError

INFO_PATH = "/v2/groups/{group_openid}/info"
STATE_PATH = "/v2/groups/{group_openid}/bot_state"
JOIN_PATH = "/v2/groups/{group_openid}/join_request_list"
MUTE_GET_PATH = "/v2/groups/{group_openid}/restrict_chat_setting"
MEMBERS_PATH = "/v2/groups/{group_openid}/members"
BLACKLIST_PATH = "/v2/groups/{group_openid}/member_blacklist"
RECALL_PATH = "/v2/groups/{group_openid}/messages/{message_id}"


class RecordingAudit:
    """记录审计调用的假实现。"""

    def __init__(self) -> None:
        self.api_calls: list[dict] = []
        self.capabilities: list[dict] = []

    def record_api_call(self, **kwargs):
        self.api_calls.append(kwargs)
        return True

    def record_capability(self, **kwargs):
        self.capabilities.append(kwargs)
        return True


class FakeClock:
    """可控时钟 + 记录 sleep 的睡眠函数。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_api(transport, **kwargs):
    clock = FakeClock()
    api = QQGroupAPI(
        transport,
        sleep=clock.sleep,
        clock=clock.monotonic,
        backoff=(0.0, 0.0),
        **kwargs,
    )
    return api, clock


def test_describe_error_mapping():
    assert describe_error(ERR_NOT_WHITELISTED)[0] == SEM_NOT_WHITELISTED
    assert describe_error(ERR_NOT_ADMIN)[0] == SEM_NOT_ADMIN
    assert describe_error(999999)[0] == "unknown"


def test_get_group_info_success_records_audit():
    transport = FakeTransport({("GET", INFO_PATH): {"group_name": "群A", "group_member_num": 12}})
    audit = RecordingAudit()
    api, _ = make_api(transport, audit=audit)
    profile = asyncio.run(api.get_group_info("g1"))
    assert profile.name == "群A"
    assert profile.member_num == 12
    assert transport.calls_for("GET", INFO_PATH)[0]["path_params"] == {"group_openid": "g1"}
    assert audit.api_calls and audit.api_calls[0]["ok"] is True
    assert audit.capabilities[0]["capability"] == CAP_GROUP_INFO
    assert audit.capabilities[0]["ok"] is True


def test_err_code_11253_maps_to_whitelist_semantic():
    transport = FakeTransport(
        {
            ("GET", INFO_PATH): {
                "err_code": ERR_NOT_WHITELISTED,
                "message": "应用无接口访问权限",
                "trace_id": "t-1",
            }
        }
    )
    audit = RecordingAudit()
    api, _ = make_api(transport, audit=audit)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    error = excinfo.value
    assert error.err_code == ERR_NOT_WHITELISTED
    assert error.semantic == SEM_NOT_WHITELISTED
    assert error.trace_id == "t-1"
    assert error.denied is True
    assert transport.calls_for("GET", INFO_PATH)[0] is not None
    assert len(transport.calls_for("GET", INFO_PATH)) == 1  # 权限类错误不重试
    assert audit.capabilities[-1]["ok"] is False
    assert audit.capabilities[-1]["err_code"] == ERR_NOT_WHITELISTED


def test_string_payload_is_parsed():
    # botpy 在 content-type 带 charset 时会返回字符串，这里必须能解析
    transport = FakeTransport(
        {("GET", INFO_PATH): json.dumps({"group_name": "群B"}, ensure_ascii=False)}
    )
    api, _ = make_api(transport)
    profile = asyncio.run(api.get_group_info("g1"))
    assert profile.name == "群B"


def test_none_payload_is_retried_then_raises_timeout():
    transport = FakeTransport({("GET", INFO_PATH): None})
    api, _clock = make_api(transport, max_retries=2)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    assert excinfo.value.semantic == SEM_TIMEOUT
    assert len(transport.calls_for("GET", INFO_PATH)) == 3  # 1 次 + 2 次重试


def test_server_error_is_retried(monkeypatch):
    transport = FakeTransport({("GET", INFO_PATH): RaisingError("boom")})
    api, _ = make_api(transport, max_retries=1)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    assert excinfo.value.retryable is True
    assert len(transport.calls_for("GET", INFO_PATH)) == 2


def test_forbidden_error_is_not_retried():
    transport = FakeTransport(
        {("GET", INFO_PATH): RaisingError("forbidden", name="ForbiddenError")}
    )
    api, _ = make_api(transport, max_retries=3)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    assert excinfo.value.denied is True
    assert len(transport.calls_for("GET", INFO_PATH)) == 1


def test_dry_run_blocks_writes_but_allows_reads():
    transport = FakeTransport(
        {
            ("GET", INFO_PATH): {"group_name": "群C"},
            ("POST", "/v2/groups/{group_openid}/restrict_chat_setting"): {},
        }
    )
    audit = RecordingAudit()
    api, _ = make_api(transport, audit=audit, dry_run_getter=lambda: True)
    assert asyncio.run(api.get_group_info("g1")).name == "群C"
    result = asyncio.run(api.mute_member("g1", "u1", seconds=600))
    assert result == {"_dry_run": True}
    assert transport.calls_for("POST", MUTE_GET_PATH) == []
    assert audit.api_calls[-1]["dry_run"] is True


def test_unavailable_transport_raises_transport_error():
    api, _ = make_api(None)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    assert excinfo.value.semantic == "transport_error"


def test_probe_reports_capabilities_and_denials():
    transport = FakeTransport(
        {
            ("GET", INFO_PATH): {"group_name": "群D", "group_member_num": 3},
            ("GET", STATE_PATH): {
                "member_role": "admin",
                "recv_msg_setting": "only_mention",
                "allow_proactive_msg": False,
            },
            ("GET", MUTE_GET_PATH): {"global_rule": {"mode": "none"}, "members": []},
            ("GET", JOIN_PATH): {"list": [], "next_cursor": ""},
            ("GET", MEMBERS_PATH): {
                "err_code": ERR_NOT_WHITELISTED,
                "message": "该能力正在内邀接入中",
            },
            ("GET", BLACKLIST_PATH): {
                "err_code": ERR_NOT_WHITELISTED,
                "message": "该能力正在内邀接入中",
            },
        }
    )
    audit = RecordingAudit()
    api, _ = make_api(transport, audit=audit)
    results = asyncio.run(api.probe("g1"))
    assert results[CAP_GROUP_INFO].ok is True
    assert results[CAP_IS_ADMIN].ok is True
    assert results[CAP_MUTE].ok is True
    assert results["full_msg"].ok is False
    assert "接收全部消息" in results["full_msg"].note
    assert results[CAP_MEMBER_LIST].ok is False
    assert results[CAP_MEMBER_LIST].err_code == ERR_NOT_WHITELISTED
    assert results[CAP_REMOVE_MEMBER].probed is False


def test_probe_marks_admin_dependent_caps_when_not_admin():
    transport = FakeTransport(
        {
            ("GET", INFO_PATH): {"group_name": "群E"},
            ("GET", STATE_PATH): {"member_role": "member", "recv_msg_setting": "all"},
            ("GET", MEMBERS_PATH): {},
            ("GET", BLACKLIST_PATH): {},
        }
    )
    api, _ = make_api(transport)
    results = asyncio.run(api.probe("g1"))
    assert results[CAP_IS_ADMIN].ok is False
    assert results[CAP_MUTE].ok is False
    assert "群管理员" in results[CAP_MUTE].note
    assert results["full_msg"].ok is True
    # 非管理员时不应调用需要管理员权限的接口
    assert transport.calls_for("GET", MUTE_GET_PATH) == []
    assert transport.calls_for("GET", JOIN_PATH) == []


def test_token_bucket_waits_when_empty():
    clock = FakeClock()
    bucket = TokenBucket(60, burst=1, clock=clock.monotonic, sleep=clock.sleep)
    asyncio.run(bucket.acquire())
    asyncio.run(bucket.acquire())
    assert clock.slept  # 第二次获取需要等待
    assert clock.slept[0] > 0


def test_permission_error_text_is_not_retried():
    """botpy 在 HTTP 400 时只透出文案，必须按文案识别为权限类错误且不重试。"""
    transport = FakeTransport(
        {("GET", INFO_PATH): RaisingError("应用无接口访问权限", name="ServerError")}
    )
    api, _ = make_api(transport, max_retries=3)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    error = excinfo.value
    assert error.semantic == SEM_NOT_WHITELISTED
    assert error.retryable is False
    assert len(transport.calls_for("GET", INFO_PATH)) == 1


def test_nested_code_field_is_used_for_semantics():
    """QQ 可能把业务码放在 code 字段（err_code 是另一套编号）。"""
    transport = FakeTransport(
        {
            ("GET", INFO_PATH): {
                "err_code": 40012010,
                "code": 11253,
                "message": "应用无接口访问权限",
            }
        }
    )
    api, _ = make_api(transport)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    assert excinfo.value.semantic == SEM_NOT_WHITELISTED
    assert len(transport.calls_for("GET", INFO_PATH)) == 1


def test_unknown_err_code_falls_back_to_message():
    transport = FakeTransport(
        {
            ("GET", MUTE_GET_PATH): {
                "err_code": 40012010,
                "message": "检查是否是管理员未通过",
            }
        }
    )
    api, _ = make_api(transport)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_restrict_setting("g1"))
    assert excinfo.value.semantic == SEM_NOT_ADMIN
    assert excinfo.value.retryable is False


def test_recall_message_url_encodes_message_id():
    """带 `/` 的 message_id（官方引用载荷的 REFIDX）必须编码，否则平台只回 404。"""
    transport = FakeTransport({("DELETE", RECALL_PATH): {"trace_id": "t1"}})
    api, _ = make_api(transport)
    asyncio.run(api.recall_message("g1", "REFIDX_/a+b", caller="command"))
    sent = transport.calls_for("DELETE", RECALL_PATH)[-1]
    assert sent["path_params"]["message_id"] == "REFIDX_%2Fa%2Bb"
    assert sent["path_params"]["group_openid"] == "g1"


def test_semantic_from_message_helper():
    from src.api_client import describe_error, semantic_from_message

    assert semantic_from_message("该接口仅白名单机器人可用")[0] == SEM_NOT_WHITELISTED
    assert semantic_from_message("无关文案")[0] == "unknown"
    assert describe_error(11253)[0] == SEM_NOT_WHITELISTED


def test_server_error_without_permission_text_is_still_retried():
    transport = FakeTransport({("GET", INFO_PATH): RaisingError("internal error")})
    api, _ = make_api(transport, max_retries=1)
    with pytest.raises(QQApiError) as excinfo:
        asyncio.run(api.get_group_info("g1"))
    assert excinfo.value.retryable is True
    assert len(transport.calls_for("GET", INFO_PATH)) == 2
