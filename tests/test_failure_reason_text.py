"""失败文案：审计 reason 必须是人类可读分类，而不是裸异常名。"""

from __future__ import annotations

from src.moderator import failure_reason_text


class _Denied(Exception):
    status_code = 403


class _Timeout(Exception):
    pass


def test_safety_refused_text() -> None:
    exc = _Denied("blocked by safety filter")
    assert failure_reason_text(exc) == "模型安全审核拒绝（内容侧风控）"


def test_client_error_text() -> None:
    exc = _Denied("invalid model parameter")
    assert failure_reason_text(exc) == "模型请求被拒（参数或权限）"


def test_timeout_text() -> None:
    assert failure_reason_text(_Timeout("timed out")) == "模型超时"


def test_unknown_falls_back_to_code() -> None:
    exc = _Timeout("weird")
    text = failure_reason_text(exc)
    assert text.startswith("模型拒绝（") and text.endswith("）")


def test_no_raw_exception_name_in_text() -> None:
    exc = _Denied("blocked by safety filter")
    assert "_Denied" not in failure_reason_text(exc)
    assert "PermissionDeniedError" not in failure_reason_text(exc)
