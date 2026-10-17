"""群内菜单的守门：menu.md（给人读的副本）不能出现真源里没有的条目。

背景：0.15.16 那批文档把「群管通知测试」加进了 menu.md，但真源是
`src/commands.py::MENU_SECTIONS`（群内文字菜单与图片菜单都由它渲染），
于是出现「文档说菜单有、机器人不显示」的不一致。这条测试专门防这个。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MENU_DOC = PLUGIN_ROOT / "menu.md"


def _menu_sections_text() -> str:
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    commands = importlib.import_module(f"{PLUGIN_ROOT.name}.src.commands")
    parts: list[str] = []
    for _title, items in commands.MENU_SECTIONS:
        parts.extend(items)
    return "\n".join(parts)


def _doc_entries() -> list[str]:
    """menu.md 里每条「• 命令 … ─ 说明」的命令片段（只取 ─ 之前）。"""
    entries: list[str] = []
    for line in MENU_DOC.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("•"):
            continue
        body = line.lstrip("•").strip().split("─")[0].strip()
        entries.append(body)
    return entries


def _doc_commands() -> list[str]:
    """每条目里的命令词（空格前的第一个词）。

    menu.md 允许比真源写得更细（例如「禁言 @某人 10分钟」vs 真源「禁言 @某人 [时长]」），
    所以只能比命令词，不能比整句——但命令词必须真的存在，否则就是「文档承诺了假指令」。
    """
    words: list[str] = []
    for entry in _doc_entries():
        word = entry.split()[0] if entry.split() else ""
        word = word.strip("（）()")
        if word and word not in words:
            words.append(word)
    return words


def test_menu_doc_has_no_command_missing_from_sections():
    sections = _menu_sections_text()
    missing = [w for w in _doc_commands() if w not in sections]
    assert not missing, (
        "menu.md 里出现了真源 MENU_SECTIONS 没有的命令（群内菜单实际不会显示它）：" + "、".join(missing)
    )


def test_menu_doc_covers_core_commands():
    commands_in_doc = _doc_commands()
    assert len(commands_in_doc) >= 12, (
        f"menu.md 抽出的命令太少（{len(commands_in_doc)}）：提取器可能退化了"
    )
    sections = _menu_sections_text()
    for core in ("群信息", "审核状态", "群管理自检"):
        assert core in sections, f"真源 MENU_SECTIONS 缺了 {core}"
        assert core in commands_in_doc, f"menu.md 缺了 {core}"

