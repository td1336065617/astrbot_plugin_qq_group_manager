"""官方族通道：包装现有 QQGroupAPI，行为与改造前完全一致。"""
from __future__ import annotations

from typing import Any

from ..models import ApplicantProfile
from ..utils import now_ts


class OfficialChannel:
    kind = "official"

    def __init__(self, api: Any, platform_id: str = "") -> None:
        self._api = api
        self.platform_id = str(platform_id or "")

    @property
    def available(self) -> bool:
        return bool(getattr(self._api, "available", False))

    def dry_run(self) -> bool:
        fn = getattr(self._api, "dry_run", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    async def get_applicant_profile(
        self, request: dict, *, caller: str = "join_review"
    ) -> dict[str, Any]:
        """官方通道只有昵称：不发起任何请求，直接返回降级画像。"""
        payload = request if isinstance(request, dict) else {}
        return ApplicantProfile(
            platform_id=self.platform_id,
            kind="official",
            user_id=str(payload.get("member_openid") or ""),
            nickname=str(payload.get("username") or ""),
            source="official_request",
            degraded=True,
            note="官方接口不提供头像/账号等级/注册时间",
            fetched_at=now_ts(),
        ).to_dict()

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_api"), name)
