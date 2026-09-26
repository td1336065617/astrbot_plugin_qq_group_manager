"""群名补全。

官方（qq_official）适配器**不给群名**：AstrBot 的 qqofficial 适配器只填 group_openid，
只有 aiocqhttp 会填 abm.group.group_name。所以官方群名只能主动调
GET /v2/groups/{group_openid}/info —— 这条路径已经由 api_client 实现，
OfficialChannel 经 __getattr__ 转发过去；OneBot 侧是 platforms/onebot.py 的原生实现。
本模块只负责「找出还没名字的群 → 逐个补 → 落盘」，与具体通道解耦。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

REFRESH_LIMIT = 50


async def refresh_group_names(
    store: Any,
    router: Any,
    *,
    limit: int = REFRESH_LIMIT,
    on_warn: Callable[[str, Exception], None] | None = None,
) -> dict[str, int]:
    """给还没有名字的群补一次群名；返回统计信息。

    只处理已有群记录、name 为空的群（手动/OneBot 事件已经拿到名字的不再请求）。
    """
    groups = list((store.groups() or {}).values())
    pending = [item for item in groups if not str(getattr(item, "name", "") or "").strip()]
    if not pending:
        return {"updated": 0, "failed": 0, "skipped": 0, "pending": 0}

    updated = 0
    failed = 0
    for config in pending[: max(1, int(limit))]:
        group_id = str(getattr(config, "group_id", "") or "")
        platform_id = str(getattr(config, "platform_id", "") or "")
        name = ""
        try:
            channel = router.channel_for(platform_id)
            profile = await channel.get_group_info(group_id, caller="webui")
            name = str(getattr(profile, "name", "") or "").strip()
        except Exception as exc:
            if on_warn is not None:
                on_warn(group_id, exc)
        if not name:
            failed += 1
            continue
        await store.ensure_group(group_id, name=name)
        updated += 1
    if updated:
        await store.flush()
    return {
        "updated": updated,
        "failed": failed,
        "skipped": max(0, len(pending) - max(1, int(limit))),
        "pending": len(pending),
    }
