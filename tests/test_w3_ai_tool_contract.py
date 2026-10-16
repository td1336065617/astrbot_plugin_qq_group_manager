"""W3-2：AI 工具契约（成员类工具提示 / 幂等作用域 / 注册名一致）。

来自优化方案 P2-2 第 4 项（0.15.2 已修）。约定：
  · 只认本模块定义的插件类（框架基类的方法也带 llm_tool 字样，必须排除）；
  · 提示在工具方法体里，幂等键在其内部实现（_ai_tool_*）里，注册名要与方法名一致。
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

MODULE = importlib.import_module(f"{ROOT.name}.main")
NOTE = "其它插件的成员"
DECORATOR = "@filter.llm_tool(name="
QUOTE = chr(34)
MEMBER_TOOLS = ("qqgm_mute_user", "qqgm_unmute_user")
MEMBER_HELPERS = ("_ai_tool_mute", "_ai_tool_unmute")


def _plugin_members() -> dict[str, str]:
    """本插件类里的方法源码（只认本模块定义的类）。"""
    out: dict[str, str] = {}
    for _, cls in vars(MODULE).items():
        if not inspect.isclass(cls) or getattr(cls, "__module__", "") != MODULE.__name__:
            continue
        for name, member in vars(cls).items():
            if not callable(member):
                continue
            try:
                out[name] = inspect.getsource(member)
            except (OSError, TypeError):  # pragma: no cover - 动态定义无源码
                continue
    return out


def _registered_name(source: str) -> str:
    """从工具方法的源码里取装饰器登记的注册名。"""
    marker = DECORATOR + QUOTE
    if marker not in source:
        return ""
    return source.split(marker, 1)[1].split(QUOTE, 1)[0]


def test_member_tools_exist():
    members = _plugin_members()
    missing = [name for name in MEMBER_TOOLS if name not in members]
    assert not missing, "成员类工具缺失（改名了？请同步本契约测试）：" + ", ".join(missing)


def test_member_tools_warn_against_other_plugins():
    members = _plugin_members()
    missing = [name for name in MEMBER_TOOLS if NOTE not in members.get(name, "")]
    assert not missing, "缺少「不要去找其它插件的成员工具」提示：" + ", ".join(missing)


def test_member_tool_helpers_keep_idempotency_scope():
    members = _plugin_members()
    missing = [name for name in MEMBER_HELPERS if "dedupe_key" not in members.get(name, "")]
    assert not missing, "写操作工具缺少 dedupe_key 幂等作用域：" + ", ".join(missing)


def test_tool_registered_names_match_methods():
    """装饰器里的注册名必须与方法名一致（名字漂移会让 AI 看到的名字和实现对不上）。"""
    mismatched = []
    for name, source in _plugin_members().items():
        registered = _registered_name(source)
        if registered and registered != name:
            mismatched.append(name + " -> " + registered)
    assert not mismatched, "工具注册名与方法名不一致：" + ", ".join(mismatched)


def test_llm_tools_confine_to_this_plugin():
    """AI 工具清单应当只有本插件的 qqgm_* 工具，不能把框架方法混进来。"""
    tool_names = sorted(
        name for name, source in _plugin_members().items() if DECORATOR in source
    )
    assert tool_names, "未找到任何 llm_tool 装饰的方法"
    assert all(name.startswith("qqgm_") for name in tool_names), "非本插件工具混入：" + str(tool_names)
