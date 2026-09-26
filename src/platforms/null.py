"""未知平台通道：全部能力降级，避免上层崩溃。"""
from __future__ import annotations

from typing import Any

from ..api_client import QQApiError
from ..models import CAPABILITIES, ApplicantProfile, CapabilityResult
from ..utils import now_ts


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

    async def get_applicant_profile(
        self, request: dict, *, caller: str = "join_review"
    ) -> dict[str, Any]:
        """未知平台：返回降级画像，绝不让审批链炸掉。"""
        return ApplicantProfile(
            platform_id=self.platform_id,
            kind="null",
            source="none",
            degraded=True,
            note="不支持的平台通道",
            fetched_at=now_ts(),
        ).to_dict()

    def __getattr__(self, name: str) -> Any:
        async def _unsupported(*args: Any, **kwargs: Any) -> Any:
            raise QQApiError(
                "不支持的平台通道",
                semantic="unsupported",
                hint="该平台没有对应的群管理接口",
            )

        return _unsupported
