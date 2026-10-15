"""QQ群管隔离用例：工具定义在入口模块、零跨插件 import、AI 公共件不依赖 astrbot。"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_zero_cross_plugin_imports() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_no_cross_plugin_imports.py"), str(ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ai_common_has_no_astrbot_import() -> None:
    tree = ast.parse((ROOT / "ai_common.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("astrbot") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not str(node.module or "").startswith("astrbot")


def test_tools_live_in_entry_module() -> None:
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    for tool in ("qqgm_group_info", "qqgm_moderation_status", "qqgm_stats", "qqgm_recent_events", "qqgm_mute_user", "qqgm_unmute_user"):
        assert tool in text, tool
    actions = (ROOT / "src" / "actions.py").read_text(encoding="utf-8")

    for name in ("def handle_manual", "MANUAL_ACTIONS", "async def _unmute"):
        assert name in actions, name


def test_manual_actions_exclude_destructive_ones() -> None:
    actions = (ROOT / "src" / "actions.py").read_text(encoding="utf-8")
    start = actions.index("MANUAL_ACTIONS: tuple[str, ...] = (")
    block = actions[start : start + 120]
    for forbidden in ("recall", "blacklist", "remove"):
        assert forbidden not in block, forbidden + " 不应出现在 MANUAL_ACTIONS"
