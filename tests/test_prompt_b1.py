"""T4a：B1 判词改动与提示词版本（prompt_version）测试。"""

from __future__ import annotations

from src.moderator import PROMPT_VERSION, SYSTEM_PROMPT_DEFAULT, resolve_prompt_version


def test_prompt_scopes_downgrade_to_ad_track():
    text = SYSTEM_PROMPT_DEFAULT
    assert "仅适用于推广/诈骗类" in text
    assert "缺任意一项 → 最多 review，severity ≤ 2" in text


def test_prompt_adds_content_body_track_with_thresholds():
    text = SYSTEM_PROMPT_DEFAULT
    assert "内容本身是否违规，与有没有渠道无关" in text
    assert "涉未成年一律 5" in text
    assert "针对特定成员的侮辱谩骂且 severity ≥ 3" in text
    assert "政治敏感：保守处理" in text


def test_prompt_keeps_context_exemption_but_not_for_porn():
    text = SYSTEM_PROMPT_DEFAULT
    assert "不适用本豁免" in text
    assert "性教育、生殖健康、医学或法律科普" in text


def test_prompt_methods_are_renumbered_without_gaps():
    text = SYSTEM_PROMPT_DEFAULT
    for num in ("1. ", "2. ", "3. ", "4. ", "5. "):
        assert num in text
    assert "severity 只描述危害大小" in text and "按轨道取" in text


def test_resolve_prompt_version_marks_custom():
    assert resolve_prompt_version(None) == PROMPT_VERSION
    assert resolve_prompt_version({"system": "自定义提示词"}) == "custom"
    assert resolve_prompt_version({"user": "自定义 user 模板"}) == "custom"
    assert resolve_prompt_version({"system": "   ", "user": ""}) == PROMPT_VERSION
