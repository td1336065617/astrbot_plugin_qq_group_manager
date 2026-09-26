"""入群申请人画像服务：缓存 / 限频 / 熔断 / 通道路由。

画像只做「加分项」：任何失败都降级返回（degraded=True），绝不阻塞或改变审批主流程。
官方通道零网络请求；OneBot 通道调 get_stranger_info。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .models import ApplicantProfile
from .utils import now_ts

#: 连续失败多少次后熔断
BREAKER_THRESHOLD = 5
#: 熔断冷却秒数
BREAKER_COOLDOWN = 300.0
#: 限频窗口（秒）
RATE_WINDOW = 60.0


class ApplicantProfileService:
    """申请人画像采集：缓存优先，限频 + 并发上限，失败熔断，全部降级安全。"""

    def __init__(
        self,
        *,
        store: Any,
        channel_for: Callable[[str], Any],
        logger: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.channel_for = channel_for
        self.logger = logger
        self._clock = clock
        self._sleep = sleep
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_size = 0
        self._calls: list[float] = []
        self._failures = 0
        self._breaker_until = 0.0
        self.stats_counters: dict[str, int] = {"hit": 0, "fetched": 0, "degraded": 0, "failed": 0}

    # ------------------------------------------------------------------
    async def get(self, group_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """返回画像 dict；本方法永不抛异常。"""
        settings = self.store.settings()
        if not settings.get("join_profile_enabled", True):
            return ApplicantProfile(degraded=True, note="画像采集已关闭").to_dict()

        payload = request if isinstance(request, dict) else {}
        config = self.store.group(group_id)
        platform_id = str(getattr(config, "platform_id", "") or "") if config is not None else ""
        uid = str(payload.get("user_id") or payload.get("member_openid") or "")
        cache_key = f"{platform_id}:{uid}"

        ttl_days = int(settings.get("join_profile_cache_days", 7) or 0)
        cached = self._cache_get(cache_key, ttl_days)
        if cached is not None:
            self.stats_counters["hit"] += 1
            cached["source"] = "cache"
            return cached

        if self._breaker_open():
            self.stats_counters["degraded"] += 1
            return ApplicantProfile(
                platform_id=platform_id,
                user_id=uid,
                degraded=True,
                note="画像服务熔断中，本次按资料缺失处理",
            ).to_dict()

        await self._wait_rate(int(settings.get("join_profile_qpm", 30) or 30))
        await self._semaphore_for().acquire()
        try:
            channel = self.channel_for(platform_id)
            profile = await channel.get_applicant_profile(payload)
            if not isinstance(profile, dict):
                raise TypeError("画像返回值不是 dict")
        except Exception as exc:
            self.stats_counters["failed"] += 1
            self._note_failure()
            return ApplicantProfile(
                platform_id=platform_id,
                user_id=uid,
                degraded=True,
                note=f"画像采集异常：{type(exc).__name__}",
            ).to_dict()
        finally:
            if self._semaphore is not None:
                self._semaphore.release()

        if profile.get("failed"):
            self.stats_counters["failed"] += 1
            self._note_failure()
        else:
            self._failures = 0
        self.stats_counters["fetched"] += 1
        if profile.get("degraded"):
            self.stats_counters["degraded"] += 1
        else:
            try:
                await self.store.put_profile(cache_key, profile)
            except Exception:  # pragma: no cover - 缓存失败不影响本次判定
                if self.logger is not None:
                    self.logger.debug("画像缓存写入失败", exc_info=True)
        return profile

    def stats(self) -> dict[str, Any]:
        return {
            "breaker_open": self._breaker_open(),
            "failures": self._failures,
            **self.stats_counters,
        }

    # ------------------------------------------------------------------
    def _cache_get(self, cache_key: str, ttl_days: int) -> dict[str, Any] | None:
        if ttl_days <= 0:
            return None
        entry = self.store.get_profile(cache_key)
        if not isinstance(entry, dict):
            return None
        fetched = int(entry.get("fetched_at") or 0)
        if fetched <= 0 or now_ts() - fetched > ttl_days * 86400:
            return None
        return dict(entry)

    def _breaker_open(self) -> bool:
        return self._clock() < self._breaker_until

    def _note_failure(self) -> None:
        self._failures += 1
        if self._failures < BREAKER_THRESHOLD:
            return
        self._failures = 0
        self._breaker_until = self._clock() + BREAKER_COOLDOWN
        if self.logger is not None:
            self.logger.warning("画像采集连续失败，熔断 %d 秒", int(BREAKER_COOLDOWN))

    def _semaphore_for(self) -> asyncio.Semaphore:
        size = int(self.store.settings().get("join_profile_concurrency", 2) or 2)
        if self._semaphore is None or self._semaphore_size != size:
            self._semaphore = asyncio.Semaphore(max(1, size))
            self._semaphore_size = size
        return self._semaphore

    async def _wait_rate(self, qpm: int) -> None:
        """滑动窗口限频：达到上限时等待，而不是丢弃申请。"""
        limit = max(1, int(qpm))
        now = self._clock()
        window = [item for item in self._calls if now - item < RATE_WINDOW]
        if len(window) >= limit:
            wait = RATE_WINDOW - (now - window[0])
            if wait > 0:
                await self._sleep(min(wait, RATE_WINDOW))
            now = self._clock()
            window = [item for item in self._calls if now - item < RATE_WINDOW]
        window.append(now)
        self._calls = window
