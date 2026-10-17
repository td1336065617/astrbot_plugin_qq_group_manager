"""指令注册守门：*_COMMANDS 里写了名字，就必须真的能被 match_command 匹配到。

背景（0.15.16 踩过）：把「群管通知测试」加进了 NOTIFY_TEST_COMMANDS，却没加进 ALL_COMMANDS，
于是这条指令永远匹配不到（死代码），而文档已经把它写成可用指令 —— 实测 match_command 返回 ("", [])。
反过来也要守：match_command 认得的每个名字都得在某个 *_COMMANDS 里有出处，避免幽灵指令。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _commands():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.src.commands")


def _declared_names() -> list[str]:
    commands = _commands()
    names: list[str] = []
    for attr in dir(commands):
        if not attr.endswith("_COMMANDS") or attr == "ALL_COMMANDS":
            continue  # ALL_COMMANDS 是汇总表，不是新出处
        value = getattr(commands, attr)
        if isinstance(value, tuple):
            names.extend(item for item in value if isinstance(item, str))
    return names


def test_every_declared_command_is_matchable():
    commands = _commands()
    names = _declared_names()
    assert len(names) >= 20, f"抽到的指令名太少（{len(names)}）：提取器可能退化了"
    dead = [name for name in names if commands.match_command(name)[0] != name]
    assert not dead, f"这些名字写在 *_COMMANDS 里但 match_command 匹配不到（死指令）：{dead}"


def test_registry_has_no_ghost_command():
    commands = _commands()
    declared = set(_declared_names())
    ghost = [name for name in commands.ALL_COMMANDS if name not in declared]
    assert not ghost, f"ALL_COMMANDS 里有出处不明的指令：{ghost}"


# 说明：不再断言「指令名唯一」——GROUP_ADMIN_COMMANDS / PUBLIC_COMMANDS 等汇总元组本来就用 * 展开基础元组，
# 同一个名字出现两次是设计，而且 match_command 是按集合成员判断，重复无害。
# 真正要守的是「写了名字必须能匹配到」和「匹配到必须有出处」。


#: 汇总表：它们由叶子元组用 * 展开而来，不参与「必须有分派」的检查
COMPOSITE_TUPLES = frozenset({"ALL_COMMANDS", "PUBLIC_COMMANDS", "GROUP_ADMIN_COMMANDS", "ADMIN_ONLY_COMMANDS"})


def test_every_leaf_command_tuple_is_dispatched():
    """能匹配到、却没有对应分派的名字会「静默空转」（用户发了没任何反应）。

    0.15.13 的死指令是「没进注册表」；反过来这一条守的是「进了注册表但没人处理」。
    """
    commands = _commands()
    main_source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    leaves = [
        attr
        for attr in dir(commands)
        if attr.endswith("_COMMANDS")
        and attr not in COMPOSITE_TUPLES
        and isinstance(getattr(commands, attr), tuple)
        and attr != "COMMANDS"  # 兼容占位（当前不存在）
    ]
    assert len(leaves) >= 15, f"叶子元组太少（{len(leaves)}）：提取器可能退化了"
    missing = [attr for attr in leaves if f"name in {attr}" not in main_source]
    assert not missing, f"这些指令元组在 main.py 里没有分派（发了没反应）：{missing}"
