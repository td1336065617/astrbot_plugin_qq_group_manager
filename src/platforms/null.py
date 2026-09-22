"""未知平台通道：全部能力降级，避免上层崩溃。"""
from __future__ import annotations

from typing import Any

from ..api_client import QQApiError
from ..models import CAPABILITIES, CapabilityResult


class NullChannel:
    kind = "null"

    def __init__(self, platform_id: str = "", *, dry_run_getter=None) -> None:
        self.platform_id = str(platform_id or "")
        self._dry_run_getter = dry_run_getter

    @property
    def available(self) -> bool:
        return False

    def dry_run(self) -> bool:
        if self._dry_run_getter is None:
            return False
        try:
            return bool(self._dry_run_getter())
        except Exception:
            return False

    async def probe(self, group_id: str, *, caller: str = "probe") -> dict[str, Any]:
        return {
            cap: CapabilityResult(capability=cap, ok=False, note="不支持的平台")
            for cap in CAPABILITIES
        }

    def __getattr__(self, name: str) -> Any:
        async def _unsupported(*args: Any, **kwargs: Any) -> Any:
            raise QQApiError(
                "不支持的平台通道",
                semantic="unsupported",
                hint="该平台没有对应的群管理接口",
            )

        return _unsupported
