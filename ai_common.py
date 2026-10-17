"""AI 工具公共件：每个插件自带一份（四仓库逐字相同，互不 import）。

本文件是**纯逻辑**：不 import astrbot、不 import 本插件其它模块，方便直接单测。
约定见《设计文档-插件AI赋能》§3：统一返回结构 + 8 个错误码 + 参数钳制/枚举/节流。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any

#: 错误码 → 默认人话（error.message 永远存在）
ERROR_CODES: dict[str, str] = {
    "invalid_argument": "参数不合法",
    "permission_denied": "没有权限执行该操作",
    "throttled": "操作太频繁，请稍后再试",
    "capability_unavailable": "平台能力暂不可用",
    "disabled": "该功能未开启",
    "not_found": "没有找到目标",
    "platform_unsupported": "当前消息通道不支持该操作",
    "internal_error": "内部错误，请稍后再试",
}


def ok(data: Any = None) -> str:
    """成功返回：{"ok": true, "data": {...}}（JSON 文本）。"""
    return json.dumps({"ok": True, "data": {} if data is None else data}, ensure_ascii=False)


def fail(code: str, message: str = "", **extra: Any) -> str:
    """失败返回：{"ok": false, "error": {"code","message",...}}。

    规则：code 不在表内 → 归一到 internal_error；message 为空 → 用默认人话；
    空值（None/""/[]/{}）的可选字段不出现（避免噪音）。
    """
    if code not in ERROR_CODES:
        code = "internal_error"
    payload: dict[str, Any] = {"code": code, "message": str(message).strip() or ERROR_CODES[code]}
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            payload[key] = value
    return json.dumps({"ok": False, "error": payload}, ensure_ascii=False)


def has_error_code(raw: str) -> str | None:
    """测试辅助：从返回文本里取 error.code（成功返回则 None）。"""
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or "") or None
    return None


def to_bool(value: Any, default: bool = True) -> bool:
    """把 "false"/"0"/"no"/"off" 也当 False（手工改配置容易写成字符串）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"false", "0", "no", "off", "none", ""}:
            return False
        if text in {"true", "1", "yes", "on"}:
            return True
        return default
    return bool(value)


def clamp_int(value: Any, low: int, high: int, default: int | None = None) -> int | None:
    """整数钳制：非数字 → default（None 表示"非法，调用方自己拒绝"）。"""
    try:
        number = int(str(value).strip())
    except Exception:
        return default
    return max(low, min(high, number))


def parse_int(value: Any, low: int, high: int) -> int | None:
    """解析整数并在范围内则返回，否则 None（用于"越界就拒绝"而不是静默钳制）。"""
    try:
        number = int(str(value).strip())
    except Exception:
        return None
    return number if low <= number <= high else None

def pick_enum(value: Any, allowed: Sequence[str], default: str | None = None) -> str | None:
    """枚举取值：大小写无关；非法 → default（None 表示"非法，调用方自己拒绝"）。"""
    text = str(value or "").strip().lower()
    for item in allowed:
        if text == item.lower():
            return item
    return default


def sanitize(text: Any, limit: int = 600) -> str:
    """清洗外部文本：去控制字符、折叠空白、截断（防注入用 neutralize，见各插件）。"""
    import re

    raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or ""))
    raw = re.sub(r"[ \t\u3000]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return raw[: max(1, int(limit))]


def mask_id(value: Any, keep_head: int = 4, keep_tail: int = 4) -> str:
    """脱敏标识：openid/QQ 号只留头尾（短值整体打码）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= keep_head + keep_tail:
        return "*" * len(text)
    hidden = "*" * (len(text) - keep_head - keep_tail)
    return f"{text[:keep_head]}{hidden}{text[-keep_tail:]}"


class ToolThrottle:
    """按 (tool, key) 计数：窗口内超过 limit 即拒。

    - check() 返回 0 表示放行，>0 表示"还需等待多少秒"（给 retry_after）；
    - note_failure() 让失败也计入（GR-04：失败刷屏同样要被限）；
    - 内部字典有上限，满了先清最老的键，避免长期占用内存。
    """

    def __init__(self, limit: int = 5, window: int = 60, max_keys: int = 4096) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window))
        self.max_keys = max(16, int(max_keys))
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._failures: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def _prune(self, key: tuple[str, str], now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        return hits

    def _shrink(self) -> None:
        if len(self._hits) <= self.max_keys:
            return
        ordered = sorted(self._hits.items(), key=lambda kv: max(kv[1] or [0.0]))
        for key, _ in ordered[: len(ordered) - self.max_keys]:
            self._hits.pop(key, None)
            self._failures.pop(key, None)

    def check(self, tool: str, key: str, now: float | None = None) -> int:
        """放行返回 0；被限流返回剩余等待秒数。"""
        stamp = time.time() if now is None else float(now)
        bucket = (str(tool), str(key))
        with self._lock:
            hits = self._prune(bucket, stamp)
            if len(hits) >= self.limit:
                oldest = min(hits)
                return max(1, int(self.window - (stamp - oldest)) + 1)
            hits.append(stamp)
            self._shrink()
        return 0

    def note_failure(self, tool: str, key: str, now: float | None = None) -> int:
        """记一次失败（失败也占一个额度，GR-04）；额度满了返回剩余等待秒数。"""
        stamp = time.time() if now is None else float(now)
        bucket = (str(tool), str(key))
        with self._lock:
            self._failures[bucket] = self._failures.get(bucket, 0) + 1
            hits = self._prune(bucket, stamp)
            hits.append(stamp)
            if len(hits) >= self.limit:
                return max(1, int(self.window - (stamp - min(hits))) + 1)
        return 0

    def failures(self, tool: str, key: str) -> int:
        return self._failures.get((str(tool), str(key)), 0)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._failures.clear()


def join_lines(items: Iterable[Any], limit: int = 40) -> str:
    """把若干条目拼成多行文本（最多 limit 条，多余省略）。"""
    rows = [str(item) for item in items if str(item or "").strip()]
    head = rows[:limit]
    if len(rows) > limit:
        head.append(f"…（共 {len(rows)} 条，已省略 {len(rows) - limit} 条）")
    return "\n".join(head)
