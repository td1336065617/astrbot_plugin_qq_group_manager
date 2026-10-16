"""WUI-2 护栏：面板可读性返工（备用模型选择器 + 纯逻辑可测）。

教训：面板文案与交互此前没有任何自动化检查，三轮后端审计也覆盖不到，
于是「已移除的旧机制」这种内部术语直接暴露给用户、备用模型把 provider 平铺成一长串复选框。
本文件把面板逻辑第一次纳入可执行测试（node 真跑纯函数）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
APP_JS = PLUGIN_ROOT / "pages" / "manage" / "app.js"
PANEL_LOGIC = PLUGIN_ROOT / "pages" / "manage" / "panel_logic.js"
SELFCHECK = PLUGIN_ROOT / "tests" / "panel_logic_selfcheck.mjs"


def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_panel_logic_selfcheck_runs_in_node():
    """面板纯逻辑必须能被 node 真跑（不需要浏览器、不需要构建工具）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node，跳过面板纯逻辑执行测试")
    proc = subprocess.run(
        [node, str(SELFCHECK)],
        capture_output=True,
        text=True,
        cwd=str(PLUGIN_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-1200:]


def test_panel_logic_module_is_pure():
    text = PANEL_LOGIC.read_text(encoding="utf-8")
    for banned in ("document.", "window.", "innerHTML"):
        assert banned not in text, "纯逻辑模块不得碰 DOM：" + banned


def test_app_imports_panel_logic():
    text = app_js()
    assert re.search(r"import\s*\{[^}]*\}\s*from\s*'\./panel_logic\.js';", text), "app.js 必须 import panel_logic.js"
    assert "selectionLabel(" in text


def test_fallback_picker_is_not_a_checkbox_wall():
    text = app_js()
    assert "function openProviderPicker(" in text
    assert "选择备用模型" in text
    assert "chip-prio" in text, "已选模型必须以带优先级的芯片呈现"
    start = text.index("function buildProviderList(")
    body = text[start:start + 3000]
    assert "class: 'field-block'" in body, "选择器外壳必须是 field-block"
    assert "provider-item" not in body, "旧的平铺复选框类名必须消失"
    assert "回退顺序（数字越小越先用）" not in body, "不得再打印原始 provider id 列表"


def test_fields_do_not_nest_labels():
    text = app_js()
    assert "text: '审核模型' }), providerSelect, fallbackList.node]" not in text, "嵌套 label 必须拆开"
    assert "card('模型'," in text, "模型与备用模型要独立成组"
