"""0.15.27：口语化粗口不算色情/辱骂（防误报），但引流、真色情、看不清的图片仍要抓住（防漏）。

审计必修项对应的两条硬约束：
- 「粗口 ⇒ allow」只在**能读懂内容**时适用；图片看不清必须 review，不得 allow（必修②）。
- 对象只认 @ / 引用（与本地 has_target 对齐）；指向明显但无 @ 无引用 ⇒ review + severity 2（必修①）。
"""

from __future__ import annotations

from src.moderator import PROMPT_VERSION, SYSTEM_PROMPT_DEFAULT

TEXT = SYSTEM_PROMPT_DEFAULT


def test_dirty_words_alone_are_not_porn() -> None:
    assert "只有脏话粗口、没有任何性内容" in TEXT
    assert "**不算色情低俗**" in TEXT
    assert "判 allow / 无 / severity 1，不要给 review" in TEXT


def test_allow_requires_readable_content() -> None:
    assert "这条只适用于你**能读懂消息内容**的情形" in TEXT
    assert "不要因为看不清就判 allow" in TEXT
    assert "按第 5 条给 review" in TEXT


def test_target_scope_matches_backend() -> None:
    assert "对象只认两种情况" in TEXT
    assert "@ 了对方" in TEXT and "引用 / 回复" in TEXT
    assert "点名" not in TEXT.split("辱骂攻击：")[1].split("违法违规：")[0], (
        "提示词不能再承诺“点名算对象”，否则与 has_target 冲突（会罚不了还发通知）"
    )


def test_obvious_but_no_mention_goes_review_not_violation() -> None:
    assert "指向明显但没有 @ 也没有引用" in TEXT
    assert "只给 review / 辱骂攻击 / severity 2" in TEXT
    assert "不要给 violation" in TEXT


def test_no_target_swearing_is_allow() -> None:
    assert "**完全没有指向对象**的粗口、情绪宣泄、吐槽、玩笑互骂不构成辱骂攻击" in TEXT
    # 有对象的辱骂仍是 violation（防漏，且与既有契约短语一致）
    assert "针对特定成员的侮辱谩骂且 severity ≥ 3 即 violation" in TEXT


def test_screenshot_examples_cover_both_directions() -> None:
    assert "太几把菜了" in TEXT
    assert "招嫖" in TEXT and "进群看片" in TEXT
    assert "色情低俗 / severity 3-5" in TEXT


def test_screenshot_does_not_exempt_advertising() -> None:
    assert "吐槽截图**不豁免引流**" in TEXT
    assert "二维码" in TEXT and "收款码" in TEXT
    assert "解析出引流目标" in TEXT


def test_prompt_version_bumped() -> None:
    assert PROMPT_VERSION == "2026-10-18-b2"
