"""在册成员名精确匹配（辱骂对象第三形式）的行为守门。"""

from __future__ import annotations

from src.utils import match_member_name


def test_exact_display_name_hits() -> None:
    assert match_member_name("张三", ["张三", "李四"]) is True


def test_at_display_name_hits() -> None:
    assert match_member_name("@张三", ["张三", "李四"]) is True


def test_substring_does_not_hit() -> None:
    assert match_member_name("张三你是个傻子", ["张三", "李四"]) is False


def test_short_name_substring_does_not_hit() -> None:
    assert match_member_name("今天风雨很大", ["风", "雨"]) is False


def test_multiple_hits_is_no_target() -> None:
    assert match_member_name("张三", ["张三", "张三"]) is False


def test_empty_roster_or_text_is_no_target() -> None:
    assert match_member_name("张三", []) is False
    assert match_member_name("张三", None) is False
    assert match_member_name("", ["张三"]) is False
