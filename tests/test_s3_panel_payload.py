"""S3 护栏：主策略面板发送的设置键必须是后端已知设置。

历史风险：面板拼错键名（例如 llm_fallback_provider_id 少了 s），
normalize/update_settings 会静默丢弃，用户以为配好了但其实没生效。
入群页已有同类契约（test_joins_settings_payload_keys_are_known_settings），
这里把主面板（含备用模型、动作地板、冷却与上限）也纳入。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.models import default_settings

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
APP_JS = PLUGIN_ROOT / "pages" / "manage" / "app.js"


def _main_panel_payload_keys() -> set[str]:
    text = APP_JS.read_text(encoding="utf-8")
    marker = "llm_fallback_provider_ids:"
    assert marker in text, "主面板 payload 里没有备用模型键（改名了？请同步本契约）"
    start = text.rfind("const payload = {", 0, text.index(marker))
    assert start >= 0, "未找到主面板 payload 起点"
    end = text.index(chr(10) + "    };", start)
    block = text[start:end]
    return set(re.findall(r"^\s{6}([a-z_]+):", block, re.M))


def _read_floor_keys() -> set[str]:
    """floor 键现在由 readFloor() 统一产出（面板显示与 payload 单一来源），契约要一并覆盖。"""
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index("const readFloor = () => ({")
    end = text.index(chr(10) + "  });", start)
    return set(re.findall(r"^\s{4}([a-z_]+):", text[start:end], re.M))


def test_main_panel_payload_keys_are_known_settings():
    keys = _main_panel_payload_keys() | _read_floor_keys()
    assert keys, "主面板 payload 解析为空"
    known = set(default_settings())
    unknown = sorted(keys - known)
    assert not unknown, f"主面板发送了后端不认识的设置键（会被静默丢弃）：{unknown}"


def test_main_panel_sends_new_fallback_and_floor_keys():
    keys = _main_panel_payload_keys() | _read_floor_keys()
    for key in (
        "llm_fallback_provider_ids",
        "review_floor_actions",
        "review_floor_categories",
        "review_floor_cooldown_seconds",
        "review_floor_daily_limit",
        "review_floor_by_category",
    ):
        assert key in keys, f"主面板缺少 payload 键：{key}"
