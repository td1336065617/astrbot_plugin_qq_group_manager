"""0.15.31：QQ 号 / 邮箱 / 昵称不是「开盒」—— 隐私口径只认可定位第三方的组合信息。"""
from __future__ import annotations

from src.moderator import PROMPT_VERSION, SYSTEM_PROMPT_DEFAULT

TEXT = SYSTEM_PROMPT_DEFAULT


def test_privacy_needs_locatable_combination() -> None:
    assert "隐私 / 「开盒」只认「可定位到具体自然人」的组合信息" in TEXT
    assert "**与第三方关联**" in TEXT
    assert "出现**两项及以上**强隐私项并指向第三方" in TEXT


def test_contact_handles_are_not_doxxing() -> None:
    assert "仅在群内出现 QQ 号、群号、QQ 昵称/ID、邮箱、游戏 ID" in TEXT
    assert "**不构成**隐私泄露，判 allow / 无 / 1" in TEXT


def test_self_disclosure_and_memes_are_not_violation() -> None:
    assert "本人主动公开自己的信息" in TEXT
    assert "114514" in TEXT
    assert "最多 review" in TEXT


def test_doxxing_examples_both_directions() -> None:
    assert "不要**判违法违规" in TEXT or "**不要**判违法违规" in TEXT
    assert "住址、身份证号、手机号、学校/单位" in TEXT
    assert "号召人肉" in TEXT


def test_prompt_version_bumped() -> None:
    assert PROMPT_VERSION == "2026-10-25-b3"


def test_old_contract_phrases_kept() -> None:
    # 既有契约（tests/test_prompt_b1.py 依赖）不得被本次改动破坏
    for phrase in (
        "仅适用于推广/诈骗类",
        "内容本身是否违规，与有没有渠道无关",
        "涉未成年一律 5",
        "针对特定成员的侮辱谩骂且 severity ≥ 3",
        "政治敏感：保守处理",
    ):
        assert phrase in TEXT



def test_grading_by_strong_items() -> None:
    assert "**强隐私项** = 真实姓名、身份证号、住址、手机号" in TEXT
    assert "出现**两项及以上**强隐私项并指向第三方" in TEXT
    assert "只有**单项**强隐私项" in TEXT and "review / 违法违规 / severity 2" in TEXT


def test_insufficient_items_must_be_allow_not_review() -> None:
    assert "**两项都不满足、也没有明确指向 ⇒ 必须 allow / 无 / 1，不要给「违法违规 review」**" in TEXT


def test_harassment_callout_is_review() -> None:
    assert "号召骚扰、网暴或集体举报**某人 ⇒ review / 其他 / severity 2-3" in TEXT


def test_third_person_fake_id_is_not_violation() -> None:
    assert "**第三人称也一样**" in TEXT
    assert "不要判 violation" in TEXT


def test_minor_priority_overrides_grading() -> None:
    assert "涉未成年一律 5」优先" in TEXT


def test_no_legacy_doxxing_phrasing() -> None:
    for bad in ("泄露他人QQ号", "开盒语境下公开传播", "涉嫌「开盒」/侵犯隐私的违法行为"):
        assert bad not in TEXT, f"提示词不应再出现旧判词：{bad}"
