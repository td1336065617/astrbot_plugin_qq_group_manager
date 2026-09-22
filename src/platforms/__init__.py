"""QQ 双通道平台层。"""
from .base import OFFICIAL_NAMES, ONEBOT_NAMES, Channel, channel_kind
from .null import NullChannel
from .official import OfficialChannel
from .onebot import OneBotChannel
from .router import ChannelRouter

__all__ = [
    "OFFICIAL_NAMES",
    "ONEBOT_NAMES",
    "Channel",
    "ChannelRouter",
    "NullChannel",
    "OfficialChannel",
    "OneBotChannel",
    "channel_kind",
]
