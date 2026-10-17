"""通知会话候选：把「平台实例 / 会话库 / 最近见过的会话」整理成可选项。

纯函数部分照抄自 `astrbot_plugin_onebot_login_guard/core/schema_options.py`
（platform_entries / session_label / session_entries / collect_db_sessions，约 40 行）——
本仓库禁止跨插件 import（tools/check_no_cross_plugin_imports.py），故复制并在原处注明出处。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MESSAGE_TYPE_LABELS = {
    "GroupMessage": "群聊",
    "FriendMessage": "私聊",
    "OtherMessage": "其他",
}


def session_label(umo: str) -> str:
    """把 unified_msg_origin 变成好读的展示名（平替版：平台 · 类型 会话）。"""
    parts = str(umo or "").split(":")
    if len(parts) >= 3:
        kind = MESSAGE_TYPE_LABELS.get(parts[1], parts[1])
        return f"{parts[0]} · {kind} {parts[2]}"
    return str(umo or "")


def session_entries(*sources: Iterable[str]) -> list[dict[str, str]]:
    """合并多个来源的 umo，去重并生成展示名。"""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        for item in source or []:
            umo = str(item or "").strip()
            if not umo or umo in seen:
                continue
            seen.add(umo)
            entries.append({"umo": umo, "label": session_label(umo)})
    return entries


def platform_entries(context: Any) -> list[dict[str, str]]:
    """枚举 AstrBot 已加载的平台实例（id 用于校验会话首段）。"""
    manager = getattr(context, "platform_manager", None)
    instances = getattr(manager, "platform_insts", None) or []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for instance in instances:
        try:
            meta = instance.meta()
        except Exception:
            continue
        pid = str(getattr(meta, "id", "") or "").strip()
        name = str(getattr(meta, "name", "") or pid).strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "name": name})
    return out


async def collect_db_sessions(context: Any, limit: int = 200) -> list[str]:
    """尽力从 AstrBot 会话库收集 umo（不同版本字段名有差异，全部容错）。"""
    getter = getattr(context, "get_db", None)
    if not callable(getter):
        return []
    try:
        db = getter()
        conversations = await db.get_all_conversations(page=1, page_size=limit)
    except Exception:
        return []
    umos: list[str] = []
    for conversation in conversations or []:
        for attr in ("user_id", "platform_id", "unified_msg_origin"):
            value = getattr(conversation, attr, None)
            if isinstance(value, str) and value.count(":") >= 2:
                umos.append(value)
                break
    return umos
