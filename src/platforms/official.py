"""官方族通道：包装现有 QQGroupAPI，行为与改造前完全一致。"""
from __future__ import annotations

from typing import Any


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

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_api"), name)
