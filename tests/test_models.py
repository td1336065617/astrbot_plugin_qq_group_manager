"""models 单元测试。"""

from __future__ import annotations

from src.models import (
    ApplicantProfile,
    BotState,
    CapabilityResult,
    GroupConfig,
    GroupProfile,
    Verdict,
    default_settings,
)


def test_bot_state_flags():
    state = BotState(group_openid="g1", member_role="admin", recv_msg_setting="all")
    assert state.is_admin is True
    assert state.full_msg is True
    member = BotState(group_openid="g1", member_role="member", recv_msg_setting="only_mention")
    assert member.is_admin is False
    assert member.full_msg is False


def test_group_profile_from_api():
    profile = GroupProfile.from_api(
        "g1",
        {
            "group_name": "读书分享会",
            "group_finger_memo": "每周一本",
            "group_class_text": "文化",
            "group_tags": ["阅读", "文学"],
            "group_member_num": "256",
        },
    )
    assert profile.name == "读书分享会"
    assert profile.member_num == 256
    assert profile.tags == ["阅读", "文学"]


def test_group_config_roundtrip_and_capability():
    config = GroupConfig.from_dict(
        {
            "group_id": "g1",
            "name": "测试群",
            "moderation_enabled": True,
            "trusted": ["u1", "", "u2"],
            "capabilities": {"mute": {"ok": True}},
        }
    )
    assert config.trusted == ["u1", "u2"]
    assert config.capability_ok("mute") is True
    assert config.capability_ok("recall") is False
    assert GroupConfig.from_dict(config.to_dict()).group_id == "g1"


def test_capability_result_probed_flag():
    result = CapabilityResult.from_dict("remove_member", {"ok": True, "probed": False})
    assert result.probed is False


def test_verdict_clamped():
    verdict = Verdict(
        verdict="maybe", category="", severity=9, confidence=3.0, reason="x" * 500
    ).clamped()
    assert verdict.verdict == "review"
    assert verdict.severity == 5
    assert verdict.confidence == 1.0
    assert len(verdict.reason) == 200
    assert verdict.is_violation is False


def test_applicant_profile_roundtrip():
    profile = ApplicantProfile(
        user_id="10001", nickname="张三", qq_level=16, account_age_days=120, avatar_url="http://a"
    )
    assert profile.has_account_signals is True
    restored = ApplicantProfile.from_dict(profile.to_dict())
    assert restored.user_id == "10001"
    assert restored.qq_level == 16
    assert restored.account_age_days == 120
    assert restored.degraded is True


def test_applicant_profile_dirty_data_is_safe():
    profile = ApplicantProfile.from_dict(
        {"qq_level": "16", "account_age_days": "x", "reg_time": None, "degraded": 0}
    )
    assert profile.qq_level == 16
    assert profile.account_age_days is None
    assert profile.reg_time is None
    assert profile.degraded is False
    assert ApplicantProfile.from_dict(None).user_id == ""
    assert ApplicantProfile.from_dict("boom").degraded is True


def test_applicant_profile_without_account_signals():
    assert ApplicantProfile(nickname="仅昵称").has_account_signals is False


def test_join_profile_settings_default_to_off():
    settings = default_settings()
    assert settings["join_profile_enabled"] is True
    assert settings["join_min_account_days"] == 0
    assert settings["join_min_qq_level"] == 0
    assert settings["join_require_qid"] is False
    assert settings["join_gate_action"] == "decline"
    assert settings["join_profile_missing"] == "manual"
    assert settings["join_avatar_review"] == "off"


def test_default_settings_is_safe():
    settings = default_settings()
    assert settings["dry_run"] is True
    assert settings["mode"] == "lenient"
    assert settings["allow_without_full_msg"] is False
    assert settings["join_review_mode"] == "off"
    settings["mute_steps"]["3"] = 1
    assert default_settings()["mute_steps"]["3"] == 600
