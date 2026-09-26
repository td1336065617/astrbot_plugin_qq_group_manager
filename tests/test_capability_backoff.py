"""能力探测的"白名单不可用"负缓存（避免每轮重复打无效请求）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api_client import QQApiError, QQGroupAPI
from src.models import CAP_MEMBER_LIST


class _Transport:
    """记录被调用的路径；/members 一律以"仅白名单可用"拒绝。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, method, path, *, path_params=None, query=None, json_body=None):
        self.calls.append(path)
        if "members" in path:
            raise QQApiError(
                "该接口仅白名单机器人可用，请向 QQ 开放平台申请权限",
                err_code=11298,
                semantic="not_whitelisted",
                hint="该接口仅白名单机器人可用，请向 QQ 开放平台申请权限",
            )
        return {"err_code": 0}

    def available(self):
        return True


def test_member_list_probe_is_not_repeated_within_ttl():
    transport = _Transport()
    api = QQGroupAPI(transport)
    first = asyncio.run(api.probe("g1"))
    assert first[CAP_MEMBER_LIST].ok is False
    calls_after_first = len([c for c in transport.calls if "members" in c])
    assert calls_after_first == 1

    second = asyncio.run(api.probe("g1"))
    assert second[CAP_MEMBER_LIST].ok is False
    assert second[CAP_MEMBER_LIST].probed is False           # 复用结论，不再打接口
    assert len([c for c in transport.calls if "members" in c]) == 1
