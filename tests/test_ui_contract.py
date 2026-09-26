"""界面与后端配置契约：防止"点了保存却被后端过滤掉"（表现为保存后恢复默认）。

历史 bug：WebUI 送审条件新增的三个选项（has_contact / ad_template / has_image）
没有同步加进 models.SEND_CONDITIONS，normalize_settings 会把它们过滤掉，
用户勾选保存后界面又变回未勾选状态。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.models import SEND_CONDITIONS, default_settings

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
APP_JS = PLUGIN_ROOT / "pages" / "manage" / "app.js"


def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_condition_labels_all_supported_by_backend():
    text = app_js()
    block = re.search(r"const CONDITION_LABELS = \{(.*?)\};", text, re.S)
    assert block, "未找到 CONDITION_LABELS 定义"
    keys = re.findall(r"^\s*([a-z_]+):", block.group(1), re.M)
    assert keys, "CONDITION_LABELS 解析为空"
    missing = [key for key in keys if key not in SEND_CONDITIONS]
    assert not missing, f"这些送审条件在界面上可选，但后端不支持（会被静默丢弃）：{missing}"


def test_policy_payload_keys_are_known_settings():
    text = app_js()
    start = text.index("async function viewPolicy")
    block = re.search(r"const payload = \{(.*?)\n    \};", text[start:], re.S)
    assert block, "未找到策略页 payload 定义"
    keys = re.findall(r"^\s{6}([a-z_]+):", block.group(1), re.M)
    assert keys, "payload 解析为空"
    known = default_settings()
    missing = [key for key in keys if key not in known]
    assert not missing, (
        f"策略页会提交这些配置，但 default_settings 未声明（保存后会被丢弃）：{missing}"
    )


def test_send_conditions_roundtrip_keeps_all_ui_options():
    from src.store import normalize_settings

    ui_options = [
        "rule_hit",
        "has_link",
        "has_contact",
        "ad_template",
        "has_image",
        "long_text",
        "new_member",
        "flood",
        "all",
    ]
    normalized = normalize_settings({"send_conditions": ui_options})["send_conditions"]
    assert normalized == ui_options


def test_domain_allowlist_settings_contract():
    from src.store import normalize_settings

    defaults = default_settings()
    assert defaults["domain_allowlist_enabled"] is True
    assert "codeforces.com" in defaults["domain_allowlist"]

    normalized = normalize_settings({"domain_allowlist_enabled": 0, "domain_allowlist": []})
    assert normalized["domain_allowlist_enabled"] is False
    assert normalized["domain_allowlist"] == []

    fallback = normalize_settings({"domain_allowlist": "codeforces.com"})
    assert isinstance(fallback["domain_allowlist"], list)
    assert "codeforces.com" in fallback["domain_allowlist"]

    cleaned = normalize_settings({"domain_allowlist": [" codeforces.com ", "", "atcoder.jp"]})
    assert cleaned["domain_allowlist"] == ["codeforces.com", "atcoder.jp"]


def test_policy_payload_includes_appeal_and_domain_keys():
    text = app_js()
    start = text.index("async function viewPolicy")
    block = re.search(r"const payload = \{(.*?)\n    \};", text[start:], re.S)
    assert block, "未找到策略页 payload 定义"
    keys = set(re.findall(r"^\s{6}([a-z_]+):", block.group(1), re.M))
    for key in (
        "domain_allowlist_enabled",
        "domain_allowlist",
        "appeal_enabled",
        "appeal_auto_whitelist",
        "appeal_notify",
    ):
        assert key in keys, f"策略页缺少 payload 键：{key}"


def test_appeals_view_contract():
    text = app_js()
    views = re.search(r"const VIEWS = \[(.*?)\];", text, re.S)
    assert views, "未找到 VIEWS 定义"
    assert "id: 'appeals'" in views.group(1), "VIEWS 缺少申诉视图"
    assert "bridge.apiGet('appeals'" in text
    assert "bridge.apiPost('appeals/decide'" in text
    assert "bridge.apiPost('appeal_whitelist'" in text
    assert "'appeal_state'" in text, "日志页缺少 appeal_state 列"

    defaults = default_settings()
    for key in ("appeal_enabled", "appeal_auto_whitelist", "appeal_notify"):
        assert key in defaults, f"default_settings 缺少 {key}"
    assert defaults["appeal_enabled"] is True
    assert defaults["appeal_auto_whitelist"] is False
    assert defaults["appeal_notify"] is True


def test_joins_settings_payload_keys_are_known_settings():
    text = app_js()
    block = re.search(r"bridge\.apiPost\('joins/settings', \{(.*?)\n      \}\);", text, re.S)
    assert block, "未找到入群审批配置 payload"
    keys = re.findall(r"^\s{8}([a-z_]+):", block.group(1), re.M)
    assert keys, "入群审批配置 payload 解析为空"
    known = default_settings()
    missing = [key for key in keys if key not in known]
    assert not missing, f"入群审批配置提交了未声明的配置键（保存后会被丢弃）：{missing}"
    for key in (
        "join_profile_enabled",
        "join_min_account_days",
        "join_min_qq_level",
        "join_require_qid",
        "join_gate_action",
        "join_profile_missing",
        "join_avatar_review",
        "join_avatar_only_below",
        "join_profile_cache_days",
        "join_profile_qpm",
        "join_profile_concurrency",
    ):
        assert key in keys, f"入群审批配置缺少 payload 键：{key}"


def test_joins_view_renders_profile_columns():
    text = app_js()
    start = text.index("async function viewJoins")
    end = text.index("async function viewAppeals", start)
    block = text[start:end]
    for token in ("'QQ等级'", "'账号年龄'", "avatar_url", "fmtProfileNumber", "fmtProfileAge"):
        assert token in block, f"入群审批视图缺少：{token}"
    assert "本通道不支持" in text, "画像缺失时未给出「本通道不支持」提示"


def test_join_profile_settings_roundtrip():
    from src.store import normalize_settings

    normalized = normalize_settings(
        {
            "join_min_account_days": 3,
            "join_min_qq_level": 8,
            "join_require_qid": True,
            "join_gate_action": "manual",
            "join_profile_missing": "manual",
            "join_avatar_review": "approve_only",
            "join_avatar_only_below": 0.9,
            "join_profile_cache_days": 3,
            "join_profile_qpm": 20,
            "join_profile_concurrency": 3,
        }
    )
    assert normalized["join_min_account_days"] == 3
    assert normalized["join_min_qq_level"] == 8
    assert normalized["join_require_qid"] is True
    assert normalized["join_gate_action"] == "manual"
    assert normalized["join_profile_missing"] == "manual"
    assert normalized["join_avatar_review"] == "approve_only"
    assert normalized["join_avatar_only_below"] == 0.9
    assert normalized["join_profile_cache_days"] == 3
    assert normalized["join_profile_qpm"] == 20
    assert normalized["join_profile_concurrency"] == 3


def test_answer_settings_survive_normalize():
    from src.store import normalize_settings

    normalized = normalize_settings(
        {
            "join_expected_answer": "  ACM  ",
            "join_answer_keywords": [" 校赛 ", "", "ACM"],
            "join_answer_regex": " ^AC\\d+$ ",
            "join_answer_action": "没这个动作",
            "join_answer_case_sensitive": 1,
        }
    )
    assert normalized["join_expected_answer"] == "ACM"
    assert normalized["join_answer_keywords"] == ["校赛", "ACM"]
    assert normalized["join_answer_regex"] == "^AC\\d+$"
    assert normalized["join_answer_action"] == "manual"
    assert normalized["join_answer_case_sensitive"] is True


def test_log_actions_first_column_is_unix():
    """BUG-017：actions 日志的时间列与后端字段一致（ts_unix），否则整列显示为空。"""
    block = re.search(r"if \(kind === 'actions'\) return \[(.*?)\];", app_js(), re.S)
    assert block
    assert block.group(1).strip().startswith("'ts_unix'")


def test_no_plan_phase_copy_and_local_time():
    """BUG-015/025：日志时间统一本地化；不再出现 M1/M2/M3 与「后续版本提供」。"""
    text = app_js()
    assert "toISOString()" not in text
    assert "toLocaleString('zh-CN', { hour12: false })" in text
    assert not re.search(r"\bM[123]\b", text)
    web = (PLUGIN_ROOT / "src" / "web_api.py").read_text(encoding="utf-8")
    assert "后续版本提供" not in web
    assert not re.search(r"\bM[123]\b", web)


def test_log_filters_follow_tab_kind():
    """BUG-029：关键字只对 events/api 生效，申诉只对 events 生效，且不残留旧条件。"""
    text = app_js()
    assert "const showKeyword = kind === 'events' || kind === 'api';" in text
    assert "const showAppealed = kind === 'events';" in text
    assert "delete state.logs.filters.keyword" in text
    assert "delete state.logs.filters.appealed" in text


def test_config_templates_accepts_list_and_others_still_reject():
    """BUG-010：templates 分区接受数组；其它分区仍要求对象。"""
    text = (PLUGIN_ROOT / "src" / "web_api.py").read_text(encoding="utf-8")
    assert 'section == "templates"' in text
    assert "templates 的 data 必须是数组" in text
    assert 'error_response("data 必须是对象")' in text


def test_db_backup_route_is_get():
    """BUG-011：新增只读 GET db/backup，前端下载走它（bridge.download 恒为 GET）。"""
    web = (PLUGIN_ROOT / "src" / "web_api.py").read_text(encoding="utf-8")
    assert 'f"/{PLUGIN_NAME}/db/backup"' in web
    assert "async def db_backup" in web
    app = app_js()
    assert "bridge.download('db/backup'" in app
    assert "bridge.download('db/maintain'" not in app


def test_maintenance_gate_uses_beijing_date():
    """BUG-043：每日维护的「当天」判定要用北京日期（不能用裸 datetime.now()）。"""
    text = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'datetime.now(CN_TZ).strftime("%Y-%m-%d")' in text
    assert 'datetime.now().strftime("%Y-%m-%d")' not in text


def test_join_review_model_selector_is_wired():
    """入群审批必须能选模型（BUG-052）：前端选择器 + 保存字段 + 后端白名单与回退。"""
    js = (PLUGIN_ROOT / "pages/manage/app.js").read_text(encoding="utf-8")
    assert "joinProviderSelect" in js
    assert "跟随发言审核模型" in js
    assert "join_llm_provider_id: joinProviderSelect.value" in js

    api = (PLUGIN_ROOT / "src/web_api.py").read_text(encoding="utf-8")
    assert '"join_llm_provider_id"' in api

    main_py = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'provider_setting="join_llm_provider_id"' in main_py
    assert 'provider_setting != "llm_provider_id"' in main_py

    models_py = (PLUGIN_ROOT / "src/models.py").read_text(encoding="utf-8")
    assert '"join_llm_provider_id"' in models_py
