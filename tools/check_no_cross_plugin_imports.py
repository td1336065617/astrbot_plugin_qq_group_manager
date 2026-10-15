"""R-IND-02 门禁：AST 扫 import + 动态 import 实参 + 其它插件目录名/数据目录。"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

SELF = pathlib.Path(__file__).resolve().parent.parent.name
ALL_PLUGINS = (
    "astrbot_plugin_qq_group_manager",
    "acmer_qq_group_bot",
    "menu_navigation",
    "来只清楚bot",
    "caidandaohang",
    "sticker_plugin",
)
OTHERS = tuple(name for name in ALL_PLUGINS if name != SELF)
PATTERN = re.compile("|".join(re.escape(name) for name in OTHERS))
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "data"}


def targets(root: pathlib.Path) -> list[pathlib.Path]:
    files = []
    for path in root.rglob("*.py"):
        # 只看**相对扫描根**的目录名：否则隔离副本（.../data/plugins/<插件>/...）会被整体跳过
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        files.append(path)
    return sorted(files)


def check_file(path: pathlib.Path) -> list[str]:
    source = path.read_text(encoding="utf-8", errors="ignore")
    problems: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{path}: 语法错误，无法检查：{exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if PATTERN.search(alias.name.split(".")[0]):
                    problems.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if PATTERN.search(module.split(".")[0]):
                problems.append(f"{path}:{node.lineno}: from {module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"import_module", "__import__", "load_module", "spec_from_file_location"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if PATTERN.search(arg.value):
                            problems.append(f"{path}:{node.lineno}: 动态 import {arg.value!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if PATTERN.search(text) and ("/" in text or "\\" in text or "data.plugins" in text):
                problems.append(f"{path}:{node.lineno}: 字符串里出现其它插件路径 {text!r}")
    return problems


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".").resolve()
    problems: list[str] = []
    for path in targets(root):
        try:
            problems.extend(check_file(path))
        except OSError:
            continue
    if problems:
        print("零耦合门禁未通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"零耦合门禁通过（{len(targets(root))} 个文件，本插件={SELF}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
