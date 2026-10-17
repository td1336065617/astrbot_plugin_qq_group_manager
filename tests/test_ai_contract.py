"""AI 契约测试：AST 检查 main.py 里的工具与 menu.md 的对应关系（不 import main.py）。"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
HASH_FILE = ROOT / "tests" / "ai_contract_hash.txt"

TOOL_PREFIX = "qqgm_"
MAX_TOOLS = 6
SUPPORTED_TYPES = {"string", "number", "object", "boolean", "array", "array[string]", "array[number]", "array[object]"}

#: 工具名 → menu.md 里的来源条目（显式映射，**不要用子串猜**）
TOOL_MENU_SOURCE: dict[str, str] = {
    "qqgm_group_info": "群信息",
    "qqgm_moderation_status": "审核状态",
    "qqgm_stats": "审核统计",
    "qqgm_recent_events": "审核日志",
    "qqgm_mute_user": "禁言",
    "qqgm_unmute_user": "解禁",
}

#: 工具名 → Args 段应有的参数个数（删一行就该红）
EXPECTED_ARGS: dict[str, int] = {
    "qqgm_group_info": 1,
    "qqgm_moderation_status": 0,   # 无参工具
    "qqgm_stats": 1,
    "qqgm_recent_events": 1,
    "qqgm_mute_user": 3,
    "qqgm_unmute_user": 1,
}

#: 有意"不给"AI 的条目（GR-07②）
EXCLUDED_COMMANDS: tuple[str, ...] = (
    "审核开启",
    "审核关闭",
    "审核模式",
    "dry-run 开启",
    "关键词 添加",
    "信任",
    "撤回",
    "入群通过",
    "黑名单 添加",
    "自检",
)
#: 这些"不给"的条目是否本来就出现在 menu.md 里（menu_navigation 的 WebUI 操作不是菜单指令 → False）
EXCLUDED_MUST_BE_IN_MENU = True


def _tool_defs() -> list[tuple[str, str, str]]:
    """返回 [(函数名, 工具名, docstring)]，按函数名排序。"""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            text = ast.unparse(deco)
            if "llm_tool" not in text:
                continue
            match = re.search("name=[\"\']([^\"\']+)[\"\']", text)
            name = match.group(1) if match else node.name
            found.append((node.name, name, ast.get_docstring(node) or ""))
    return sorted(found)


def _args_section(doc: str) -> list[tuple[str, str]]:
    """解析 Args 段 → [(参数名, 类型)]。"""
    if "Args:" not in doc:
        return []
    entries = []
    for line in doc.split("Args:", 1)[1].splitlines():
        text = line.strip()
        if not text:
            continue
        match = re.match(r"^([A-Za-z_][\w]*)\s*\(([^)]+)\)", text)
        if match:
            entries.append((match.group(1), match.group(2).strip()))
    return entries


def test_tool_count_within_budget() -> None:
    assert len(_tool_defs()) <= MAX_TOOLS


def test_tool_names_prefixed() -> None:
    for _, tool_name, _ in _tool_defs():
        assert tool_name.startswith(TOOL_PREFIX), tool_name


def test_docstring_args_parsable() -> None:
    """Args 段必须能解析：类型在白名单内；签名不是 **kwargs 时参数名必须在签名里。"""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    sig = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            sig[node.name] = (names, node.args.kwarg is not None or node.args.vararg is not None)
    for func, tool_name, doc in _tool_defs():
        expected = EXPECTED_ARGS.get(tool_name)
        if expected == 0:
            # 无参工具：允许没有 Args 段（框架对无参工具不解析参数）
            assert _args_section(doc) == [], f"{tool_name} 声明为无参，却写了 Args"
            continue
        assert "Args:" in doc, f"{tool_name} 缺 Args 段（参数会被静默丢弃）"
        entries = _args_section(doc)
        assert entries, f"{tool_name} 的 Args 段解析不出任何参数"
        for arg_name, arg_type in entries:
            assert arg_type in SUPPORTED_TYPES, f"{tool_name}.{arg_name} 类型非法: {arg_type}"
            names, has_kwargs = sig.get(func, (set(), False))
            if not has_kwargs:
                assert arg_name in names, f"{tool_name}.{arg_name} 未出现在函数签名里"


def test_args_count_matches_declaration() -> None:
    """每个工具的参数个数都要显式声明（少写/多写一行 Arg → 红）。"""
    for _, tool_name, doc in _tool_defs():
        assert tool_name in EXPECTED_ARGS, f"{tool_name} 未在 EXPECTED_ARGS 里声明参数个数"
        assert len(_args_section(doc)) == EXPECTED_ARGS[tool_name], f"{tool_name} 的 Args 行数与声明不符"


def test_every_tool_declares_menu_source() -> None:
    declared = set(TOOL_MENU_SOURCE)
    registered = {tool_name for _, tool_name, _ in _tool_defs()}
    assert registered == declared, f"未声明来源: {registered - declared}；多余声明: {declared - registered}"


def test_menu_covers_tools() -> None:
    menu = (ROOT / "menu.md").read_text(encoding="utf-8")
    for tool_name, source in TOOL_MENU_SOURCE.items():
        assert source in menu, f"{tool_name} 的来源条目「{source}」不在 menu.md 里"


def test_exclusions_declared_and_not_implemented() -> None:
    """排除名单必须显式声明，且不得被做成工具。"""
    assert EXCLUDED_COMMANDS, "排除名单不能为空（GR-07②）"
    menu = (ROOT / "menu.md").read_text(encoding="utf-8") if (ROOT / "menu.md").is_file() else ""
    for item in EXCLUDED_COMMANDS:
        if EXCLUDED_MUST_BE_IN_MENU:
            assert item in menu, f"排除项「{item}」不在菜单里，无法证明它是有意排除的"
        assert item not in TOOL_MENU_SOURCE.values(), f"排除项「{item}」却被映射成了工具来源"


def _schema_digest() -> str:
    digest = hashlib.sha256()
    for func, tool_name, doc in _tool_defs():
        digest.update(f"{func}|{tool_name}|{doc}".encode())
    return digest.hexdigest()


def test_schema_hash_locked() -> None:
    """签名 + docstring 锁定。首次生成：UPDATE_AI_CONTRACT_HASH=1 python -m pytest tests/test_ai_contract.py"""
    expected = _schema_digest()
    if os.environ.get("UPDATE_AI_CONTRACT_HASH") == "1":
        HASH_FILE.write_text(expected + "\n", encoding="utf-8")
        return
    assert HASH_FILE.is_file(), "缺 tests/ai_contract_hash.txt，先跑一次 UPDATE_AI_CONTRACT_HASH=1"
    assert HASH_FILE.read_text(encoding="utf-8").strip() == expected, "工具签名或 docstring 变了"
