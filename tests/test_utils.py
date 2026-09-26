"""utils 单元测试。"""

from __future__ import annotations

from src.utils import (
    clamp_float,
    clamp_int,
    digest_text,
    human_duration,
    mask_openid,
    normalize_command,
    parse_duration,
    parse_iso,
    split_command,
    to_iso,
    truncate,
)


def test_parse_duration_variants():
    assert parse_duration("10分钟") == 600
    assert parse_duration("30s") == 30
    assert parse_duration("1小时") == 3600
    assert parse_duration("2天") == 172800
    assert parse_duration("10") == 600  # 纯数字文本按分钟解释
    assert parse_duration(45) == 45  # 数字类型按秒解释
    assert parse_duration("半个小时") is None
    assert parse_duration("") is None


def test_human_duration():
    assert human_duration(600) == "10 分钟"
    assert human_duration(3660) == "1 小时1 分钟"
    assert human_duration(0) == "0 秒"


def test_iso_roundtrip():
    ts = parse_iso("2026-09-10T12:00:00+08:00")
    assert ts is not None
    assert to_iso(ts) == "2026-09-10T12:00:00+08:00"
    assert parse_iso("2026-09-10T04:00:00Z") == ts
    assert parse_iso("not-a-time") is None


def test_command_normalization():
    assert normalize_command("/群信息") == "群信息"
    assert normalize_command("  群信息  ") == "群信息"
    assert normalize_command("禁言   @张三   10分钟") == "禁言 @张三 10分钟"
    assert split_command("/禁言 @张三 10分钟") == ("禁言", ["@张三", "10分钟"])


def test_text_helpers():
    assert mask_openid("ABCDEFGHIJKLMNOP") == "ABCDEF…MNOP"
    assert mask_openid("short") == "short"
    assert truncate("abcdef", 4) == "abc…"
    assert digest_text("hello") == digest_text("hello")
    assert digest_text("hello") != digest_text("hello!")


def test_clamp_helpers():
    assert clamp_int("5", 1, 1, 10) == 5
    assert clamp_int("99", 1, 1, 10) == 10
    assert clamp_int("x", 7, 1, 10) == 7
    assert clamp_int(True, 7, 1, 10) == 7
    assert clamp_float(1.5, 0.0, 0.0, 1.0) == 1.0
    assert clamp_float("bad", 0.5, 0.0, 1.0) == 0.5


def test_mask_uid_covers_qq_numbers_and_openids():
    from src.utils import mask_uid

    assert mask_uid("10001") == "100…01"
    assert mask_uid("A" * 32) == "AAAAAA…AAAA"
    assert mask_uid("123") == "123"
    assert mask_uid(None) == ""
    assert "10001" not in mask_uid("10001")
