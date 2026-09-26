"""通用工具：时间、文本、时长解析、数值钳制。

本模块只依赖标准库，便于在没有 AstrBot 运行时的情况下做单元测试。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

#: 东八区（QQ 平台的 RFC3339 时间戳均为 +08:00）
CN_TZ = timezone(timedelta(hours=8))

_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "秒": 1,
    "m": 60,
    "分": 60,
    "分钟": 60,
    "h": 3600,
    "时": 3600,
    "小时": 3600,
    "d": 86400,
    "天": 86400,
}
_DURATION_RE = re.compile(r"(\d+)\s*(分钟|小时|秒|分|时|天|s|m|h|d)", re.IGNORECASE)


def now_ts() -> int:
    """当前 Unix 时间戳（秒）。"""
    return int(time.time())


def to_iso(ts: float | int | None = None) -> str:
    """把时间戳格式化为东八区 RFC3339 字符串。"""
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts), tz=CN_TZ).isoformat(timespec="seconds")


def parse_iso(value: Any) -> int | None:
    """解析 RFC3339 / ISO8601 字符串为 Unix 时间戳，失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return int(dt.timestamp())


def digest_text(text: str, *, limit: int = 512) -> str:
    """对消息文本取摘要（只取前 limit 个字符），用于去重与缓存键。"""
    payload = (text or "")[:limit].encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:32]


def truncate(text: Any, limit: int = 120) -> str:
    """截断文本，超出长度补省略号。"""
    value = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def mask_openid(openid: Any) -> str:
    """脱敏展示 openid：保留前 6 位与后 4 位。"""
    value = "" if openid is None else str(openid)
    if len(value) <= 12:
        return value
    return value[:6] + "…" + value[-4:]


def mask_uid(value: Any) -> str:
    """脱敏用户标识：QQ 号（短数字）与 openid（长串）都适用。

    mask_openid 只处理超长 openid；OneBot 通道的 member_openid 就是真实 QQ 号，
    长度不足以触发脱敏，因此群内文案与通知统一改用本函数。
    """
    text = "" if value is None else str(value)
    if len(text) <= 4:
        return text
    if len(text) <= 12:
        return text[:3] + "…" + text[-2:]
    return text[:6] + "…" + text[-4:]


def parse_duration(text: Any) -> int | None:
    """解析时长，统一返回秒数。

    规则：
    - 数字类型输入：按「秒」解释（便于程序内部传递）；
    - 带单位的文本：按单位换算（10分钟 / 1小时 / 2天 / 30s）；
    - 纯数字文本：按「分钟」解释（贴合群内使用习惯，如「禁言 @某人 10」）。
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text)
    value = str(text).strip().lower()
    if not value:
        return None
    if value.isdigit():
        return int(value) * 60
    match = _DURATION_RE.fullmatch(value)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    factor = _DURATION_UNITS.get(unit)
    if factor is None:
        return None
    return amount * factor


def human_duration(seconds: int | None) -> str:
    """把秒数格式化为中文时长。"""
    if seconds is None:
        return "永久"
    remain = int(seconds)
    if remain <= 0:
        return "0 秒"
    parts: list[str] = []
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60), ("秒", 1)):
        if remain >= size:
            count, remain = divmod(remain, size)
            parts.append(f"{count} {unit}")
        if len(parts) >= 2:
            break
    return "".join(parts) if parts else f"{seconds} 秒"


def normalize_command(text: Any) -> str:
    """归一化指令文本：去首尾空白、压缩空白、去掉一个前导斜杠。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if value.startswith("/"):
        value = value[1:].lstrip()
    return value


def split_command(text: Any) -> tuple[str, list[str]]:
    """把 "禁言 @某人 10分钟" 拆成 ("禁言", ["@某人", "10分钟"])。"""
    normalized = normalize_command(text)
    if not normalized:
        return "", []
    parts = normalized.split(" ")
    return parts[0], parts[1:]


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把任意输入钳制为合法整数；非法输入返回默认值。"""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    """把任意输入钳制为合法浮点数；非法输入返回默认值。"""
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def optional_int(value: Any) -> int | None:
    """尽力把任意值转成 int，失败返回 None（画像字段「宽进严出」用）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def safe_json_dumps(value: Any, *, limit: int = 2000) -> str:
    """安全序列化（用于写审计库），失败时退化为字符串。"""
    try:
        dumped = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        dumped = str(value)
    return dumped[:limit]
