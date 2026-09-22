"""通道路由：按事件/平台实例选择官方族或 OneBot 通道。"""
from __future__ import annotations

from typing import Any

from ..api_client import BotpyTransport
from .base import channel_kind
from .null import NullChannel
from .official import OfficialChannel
from .onebot import OneBotChannel


class ChannelRouter:
    """对上层暴露与 QQGroupAPI 一致的方法；内部按当前事件切换通道。"""

    def __init__(
        self,
        base_api: Any,
        *,
        dry_run_getter=None,
        logger=None,
        context=None,
    ) -> None:
        object.__setattr__(self, "_base", base_api)
        object.__setattr__(self, "_dry_run_getter", dry_run_getter)
        object.__setattr__(self, "_logger", logger)
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_channels", {})
        object.__setattr__(self, "_active", OfficialChannel(base_api, ""))

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_active"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_base"), name, value)

    @property
    def available(self) -> bool:
        return bool(getattr(self._active, "available", False))

    @property
    def kind(self) -> str:
        return str(getattr(self._active, "kind", ""))

    @property
    def platform_id(self) -> str:
        return str(getattr(self._active, "platform_id", ""))

    def dry_run(self) -> bool:
        fn = getattr(self._active, "dry_run", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                pass
        base = object.__getattribute__(self, "_base")
        fn = getattr(base, "dry_run", None)
        return bool(fn()) if callable(fn) else False

    @property
    def audit(self) -> Any:
        return getattr(object.__getattribute__(self, "_base"), "audit", None)

    @property
    def transport(self) -> Any:
        return getattr(object.__getattribute__(self, "_base"), "transport", None)

    def _name_of(self, platform_id: str) -> str:
        target = str(platform_id or "")
        if not target:
            return ""
        context = object.__getattribute__(self, "_context")
        getter = getattr(context, "get_platform_inst", None)
        inst = None
        if callable(getter):
            try:
                inst = getter(target)
            except Exception:
                inst = None
        if inst is None:
            return ""
        try:
            meta = inst.meta()
        except Exception:
            return ""
        return str(getattr(meta, "name", "") or "")

    def _bot_of_platform(self, platform_id: str) -> Any:
        target = str(platform_id or "")
        if not target:
            return None
        context = object.__getattribute__(self, "_context")
        getter = getattr(context, "get_platform_inst", None)
        inst = None
        if callable(getter):
            try:
                inst = getter(target)
            except Exception:
                inst = None
        if inst is None:
            return None
        bot = getattr(inst, "bot", None) or getattr(inst, "client", None)
        if bot is None:
            getter2 = getattr(inst, "get_client", None)
            if callable(getter2):
                try:
                    bot = getter2()
                except Exception:
                    bot = None
        return bot

    def _onebot_channel(self, platform_id: str, *, event: Any = None) -> OneBotChannel:
        channels = object.__getattribute__(self, "_channels")
        channel = channels.get(platform_id)
        bot = getattr(event, "bot", None) if event is not None else None
        if bot is None:
            bot = self._bot_of_platform(platform_id)
        if not isinstance(channel, OneBotChannel):
            channel = OneBotChannel(
                bot,
                platform_id,
                dry_run_getter=object.__getattribute__(self, "_dry_run_getter"),
                logger=object.__getattribute__(self, "_logger"),
            )
            channels[platform_id] = channel
        if bot is not None:
            channel.bot = bot
        if event is not None and not channel.self_id:
            try:
                channel.self_id = str(event.get_self_id() or "")
            except Exception:
                pass
        return channel

    def _activate(self, name: str, platform_id: str, *, event: Any = None) -> Any:
        kind = channel_kind(name)
        if kind == "official":
            if event is not None:
                transport = BotpyTransport.from_event(event)
                if transport is not None and transport.available:
                    object.__getattribute__(self, "_base").transport = transport
            active = OfficialChannel(object.__getattribute__(self, "_base"), platform_id)
        elif kind == "onebot":
            active = self._onebot_channel(platform_id, event=event)
        else:
            active = NullChannel(
                platform_id,
                dry_run_getter=object.__getattribute__(self, "_dry_run_getter"),
            )
        object.__setattr__(self, "_active", active)
        return active

    def bind_event(self, event: Any) -> Any:
        name = ""
        platform_id = ""
        try:
            name = str(event.get_platform_name() or "")
        except Exception:
            name = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""
        return self._activate(name, platform_id, event=event)

    def bind_platform(self, platform_id: str) -> Any:
        return self._activate(self._name_of(platform_id), str(platform_id or ""))

    def channel_for(self, platform_id: str) -> Any:
        name = self._name_of(platform_id)
        kind = channel_kind(name)
        if kind == "official":
            return OfficialChannel(object.__getattribute__(self, "_base"), platform_id)
        if kind == "onebot":
            return self._onebot_channel(str(platform_id or ""))
        return NullChannel(
            platform_id, dry_run_getter=object.__getattribute__(self, "_dry_run_getter")
        )

    def kind_for(self, platform_id: str) -> str:
        return channel_kind(self._name_of(platform_id))
