"""菜单文案守门：菜单里该出现的取值/限制，不能因为改写而丢掉。

背景：0.15.17 之前菜单若干描述与实际行为不符（例如「撤回」只写了 2 分钟限制，
没写「群主/管理员的消息撤不了」和「官方通道纯图片引用可能定位不到」）。
文案正确性没法全自动判定，但**枚举取值**与**关键限制词**可以钉住。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _modules():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    prefix = PLUGIN_ROOT.name
    return (
        importlib.import_module(f"{prefix}.src.commands"),
        importlib.import_module(f"{prefix}.src.models"),
    )


def test_menu_lists_every_moderation_mode():
    commands, models = _modules()
    text = commands.menu_text()
    zh = {"strict": "严格", "standard": "标准", "lenient": "宽松", "log_only": "仅记录"}
    missing = [zh[m] for m in models.MODERATION_MODES if zh[m] not in text]
    assert not missing, f"菜单没写这些处置模式：{missing}"
    assert "跟随" in text, "菜单没写「审核模式 跟随」（清除群级覆盖的唯一入口）"


def test_menu_lists_every_join_review_mode():
    commands, models = _modules()
    text = commands.menu_text()
    zh = {"off": "关闭", "strict": "严格", "standard": "标准", "human": "人工"}
    missing = [zh[m] for m in models.JOIN_REVIEW_MODES if zh[m] not in text]
    assert not missing, f"菜单没写这些人群审批模式：{missing}"


def test_recall_description_keeps_key_limits():
    commands, _ = _modules()
    text = commands.menu_text()
    line = next((item for item in text.splitlines() if "撤回" in item), "")
    assert line, "菜单里找不到「撤回」条目"
    for key in ("2 分钟", "群主", "普通成员", "图片", "机器人", "群管"):
        assert key in line, f"「撤回」说明丢了关键限制「{key}」：{line}"


def test_menu_mentions_full_message_prerequisite():
    commands, _ = _modules()
    text = commands.menu_text()
    assert "接收全部消息" in text, "菜单没写「需群已开启接收全部消息」这个前置条件"



def _render():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.src.menu_render")


def test_every_menu_item_gets_a_chip():
    """每一行都必须渲染出指令胶囊。

    命令段写得过长（例如把多种写法并列写进去）会让整行退化成正文，看着像「掉渲染」——
    0.15.18 之前就有三行是这样（审核模式 / 黑名单 / 全局拉黑）。
    """
    commands, _ = _modules()
    render = _render()
    bad = [
        item
        for _title, items in commands.MENU_SECTIONS
        for item in items
        if not render.split_item(item)[0]
    ]
    assert not bad, f"这些菜单行没有指令胶囊（会看着像掉渲染）：{bad}"
