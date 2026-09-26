"""QQ 官方开放平台「群聊管理」接口客户端。

设计要点（依据 docs/平台能力调研.md）：
- **唯一出网点**：所有 QQ API 调用都经过本模块，便于限频、审计、dry-run 与 mock 测试。
- **传输层可注入**：生产用 BotpyTransport（复用 botpy 已持有的 access_token），
  测试用 FakeTransport（见 tests/fakes.py）。
- botpy 的 HTTP 层在非 2xx 时只抛出 message 字符串（响应体里的 err_code 会丢失），
  因此这里同时按「HTTP 状态」与「响应体里的 err_code」做语义映射。
- botpy 在超时/连接重置时可能返回 None（不抛异常），这里统一视为可重试失败。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol, runtime_checkable

from .models import (
    BLACKLIST_PAGE_MAX,
    CAP_BLACKLIST,
    CAP_BOT_STATE,
    CAP_FULL_MSG,
    CAP_GROUP_INFO,
    CAP_IS_ADMIN,
    CAP_JOIN_REVIEW,
    CAP_MEMBER_LIST,
    CAP_MUTE,
    CAP_RECALL,
    CAP_REMOVE_MEMBER,
    ERR_ADMIN_CHECK_FAILED,
    ERR_GROUP_GONE,
    ERR_INTERFACE_FORBIDDEN,
    ERR_MSG_ID_EXPIRED,
    ERR_NOT_ADMIN,
    ERR_NOT_MEMBER,
    ERR_NOT_WHITELISTED,
    ERR_PRIVILEGE_CHECK_FAILED,
    ERR_PROACTIVE_LIMIT,
    ERR_RATE_LIMITED,
    ERR_RECALL_EXPIRED,
    ERR_RECALL_FORBIDDEN,
    ERR_REQ_INVALID,
    ERR_ROBOT_BANNED,
    JOIN_REQUEST_PAGE_MAX,
    MUTE_BATCH_MAX,
    BotState,
    CapabilityResult,
    GroupProfile,
)
from .utils import clamp_int, now_ts, parse_iso, to_iso

# --------------------------------------------------------------------------
# 语义错误
# --------------------------------------------------------------------------
SEM_NOT_WHITELISTED = "not_whitelisted"
SEM_NOT_ADMIN = "not_admin"
SEM_ROBOT_BANNED = "robot_banned"
SEM_FORBIDDEN = "forbidden"
SEM_NOT_FOUND = "not_found"
SEM_RATE_LIMITED = "rate_limited"
SEM_RECALL_EXPIRED = "recall_expired"
SEM_RECALL_FORBIDDEN = "recall_forbidden"
SEM_MSG_EXPIRED = "msg_id_expired"
SEM_PROACTIVE_LIMIT = "proactive_limit"
SEM_NOT_MEMBER = "not_member"
SEM_GROUP_GONE = "group_gone"
SEM_INVALID = "invalid_request"
SEM_SERVER = "server_error"
SEM_TIMEOUT = "timeout"
SEM_TRANSPORT = "transport_error"
SEM_DISABLED = "disabled"
SEM_UNKNOWN = "unknown"

ERR_SEMANTICS: dict[int, tuple[str, str]] = {
    ERR_NOT_WHITELISTED: (
        SEM_NOT_WHITELISTED,
        "该接口仅白名单机器人可用，请向 QQ 开放平台申请权限",
    ),
    ERR_INTERFACE_FORBIDDEN: (SEM_NOT_WHITELISTED, "接口被封禁，请联系平台运营"),
    ERR_PRIVILEGE_CHECK_FAILED: (SEM_SERVER, "应用权限检查失败（系统错误），可重试一次"),
    ERR_ADMIN_CHECK_FAILED: (SEM_SERVER, "管理员检查失败（系统错误），可重试一次"),
    ERR_NOT_ADMIN: (SEM_NOT_ADMIN, "机器人未被授予群管理员，请先在群内设置"),
    ERR_ROBOT_BANNED: (SEM_ROBOT_BANNED, "机器人已被封禁，请停止调用并联系平台"),
    ERR_NOT_MEMBER: (SEM_NOT_MEMBER, "机器人不在该群内"),
    ERR_GROUP_GONE: (SEM_GROUP_GONE, "该群已失效或不存在"),
    ERR_RATE_LIMITED: (SEM_RATE_LIMITED, "触发限频，请降低调用频率"),
    ERR_RECALL_EXPIRED: (SEM_RECALL_EXPIRED, "消息发送超过 2 分钟，无法撤回"),
    ERR_RECALL_FORBIDDEN: (SEM_RECALL_FORBIDDEN, "无撤回权限（需要群管理员）"),
    ERR_MSG_ID_EXPIRED: (SEM_MSG_EXPIRED, "被动回复的 msg_id 已过期"),
    ERR_PROACTIVE_LIMIT: (SEM_PROACTIVE_LIMIT, "主动消息超出频控限制"),
    ERR_REQ_INVALID: (SEM_INVALID, "请求体不合法（多为插件构造问题）"),
}

STATUS_SEMANTICS: dict[int, tuple[str, str]] = {
    401: (SEM_NOT_WHITELISTED, "鉴权失败，access_token 可能失效"),
    403: (SEM_FORBIDDEN, "无权限调用该接口"),
    404: (SEM_NOT_FOUND, "接口或资源不存在"),
    405: (SEM_INVALID, "HTTP 方法不被允许"),
    429: (SEM_RATE_LIMITED, "触发平台限频"),
    500: (SEM_SERVER, "平台内部错误"),
    504: (SEM_SERVER, "平台处理超时"),
}


#: 平台在 HTTP 4xx 时只把 message 透出（err_code 会丢），这里按文案兜底识别
TEXT_SEMANTICS: tuple[tuple[str, tuple[str, str]], ...] = (
    (
        "应用无接口访问权限",
        (SEM_NOT_WHITELISTED, "该接口仅白名单机器人可用，请向 QQ 开放平台申请权限"),
    ),
    ("仅白名单", (SEM_NOT_WHITELISTED, "该接口仅白名单机器人可用，请向 QQ 开放平台申请权限")),
    ("机器人应用未获得调用该接口的权限", (SEM_NOT_WHITELISTED, "需要向 QQ 开放平台申请该接口权限")),
    ("检查是否是管理员未通过", (SEM_NOT_ADMIN, "机器人未被授予群管理员，请先在群内设置")),
    ("无操作权限", (SEM_FORBIDDEN, "机器人没有该操作的权限（多为非群管理员）")),
    ("已超出消息撤回时限", (SEM_RECALL_EXPIRED, "消息发送超过 2 分钟，无法撤回")),
    ("已被封禁", (SEM_ROBOT_BANNED, "机器人已被封禁，请停止调用并联系平台")),
    ("不是群成员", (SEM_NOT_MEMBER, "机器人不在该群内")),
)


def semantic_from_message(message: str) -> tuple[str, str]:
    """按错误文案推断语义（用于 botpy 丢失 err_code 的场景）。"""
    text = message or ""
    for keyword, (semantic, hint) in TEXT_SEMANTICS:
        if keyword in text:
            return semantic, hint
    return SEM_UNKNOWN, ""


def describe_error(err_code: int | None) -> tuple[str, str]:
    """把 QQ err_code 映射为 (语义, 处置建议)。"""
    if err_code is None:
        return SEM_UNKNOWN, ""
    return ERR_SEMANTICS.get(err_code, (SEM_UNKNOWN, f"未知错误码 {err_code}"))


class QQApiError(Exception):
    """QQ 开放平台调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        err_code: int | None = None,
        semantic: str = SEM_UNKNOWN,
        hint: str = "",
        trace_id: str = "",
        http_status: int | None = None,
        method: str = "",
        path: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message or hint or "QQ API 调用失败")
        self.message = message or hint or "QQ API 调用失败"
        self.err_code = err_code
        self.semantic = semantic
        self.hint = hint
        self.trace_id = trace_id
        self.http_status = http_status
        self.method = method
        self.path = path
        self.retryable = retryable

    @property
    def denied(self) -> bool:
        """是否属于"平台未授权/未开放"类失败（需要留痕而非重试）。"""
        return self.semantic in {
            SEM_NOT_WHITELISTED,
            SEM_NOT_ADMIN,
            SEM_FORBIDDEN,
            SEM_ROBOT_BANNED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "err_code": self.err_code,
            "semantic": self.semantic,
            "hint": self.hint,
            "trace_id": self.trace_id,
            "http_status": self.http_status,
            "method": self.method,
            "path": self.path,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        parts = [self.message]
        if self.err_code is not None:
            parts.append(f"err_code={self.err_code}")
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        if self.hint:
            parts.append(f"建议：{self.hint}")
        return " | ".join(parts)


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------
@runtime_checkable
class Transport(Protocol):
    """QQ 官方接口传输层协议。"""

    async def request(
        self,
        method: str,
        path: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """发起一次请求，返回已解析的响应（dict / str / None）。"""
        ...


class BotpyTransport:
    """生产传输层：复用 botpy 的 Route + BotHttp（AstrBot 已持有 access_token）。

    用法：
        transport = BotpyTransport.from_event(event)      # 事件上下文中
        transport = BotpyTransport.from_platform(inst)    # 定时任务中（平台实例）
    """

    def __init__(self, http: Any) -> None:
        self._http = http

    @property
    def available(self) -> bool:
        return self._http is not None

    @classmethod
    def from_event(cls, event: Any) -> BotpyTransport | None:
        bot = getattr(event, "bot", None)
        http = getattr(getattr(bot, "api", None), "_http", None)
        return cls(http) if http is not None else None

    @classmethod
    def from_platform(cls, platform: Any) -> BotpyTransport | None:
        client = getattr(platform, "client", None)
        http = getattr(getattr(client, "api", None), "_http", None)
        return cls(http) if http is not None else None

    async def request(
        self,
        method: str,
        path: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        from botpy.http import Route  # 延迟导入：测试环境无需安装 botpy

        route = Route(method, path, **(path_params or {}))
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if query:
            kwargs["params"] = {k: v for k, v in query.items() if v is not None}
        return await self._http.request(route, **kwargs)


# --------------------------------------------------------------------------
# 限频
# --------------------------------------------------------------------------
class TokenBucket:
    """简单令牌桶：按分钟速率限流，sleep 可注入以便测试。"""

    def __init__(
        self,
        rate_per_minute: float,
        *,
        burst: float | None = None,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.rate = max(0.001, float(rate_per_minute)) / 60.0
        self.capacity = float(burst) if burst else max(1.0, self.rate * 5)
        self._tokens = self.capacity
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """获取令牌，必要时等待。"""
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated)
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            await self._sleep(min(wait, 5.0))


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class QQGroupAPI:
    """QQ 群管理接口封装（Route 路径与官方文档一致）。"""

    def __init__(
        self,
        transport: Transport | None,
        *,
        audit: Any = None,
        dry_run_getter: Any = None,
        per_group_qpm: float = 60,
        global_qpm: float = 180,
        max_retries: int = 2,
        backoff: tuple[float, ...] = (0.5, 1.5),
        sleep: Any = asyncio.sleep,
        clock: Any = time.monotonic,
        capability_log_dedupe: float = 1800.0,
    ) -> None:
        self.transport = transport
        self.audit = audit
        self._dry_run_getter = dry_run_getter
        self._global_bucket = TokenBucket(global_qpm, sleep=sleep, clock=clock)
        self._group_buckets: dict[str, TokenBucket] = {}
        self._per_group_qpm = per_group_qpm
        self._max_retries = max(0, int(max_retries))
        self._backoff = backoff
        self._sleep = sleep
        self._clock = clock
        self._cap_dedupe = max(0.0, float(capability_log_dedupe))
        self._cap_log_at: dict[tuple[str, str, bool], float] = {}
        # 白名单/权限类失败的能力：一段时间内不再重复探测（避免对腾讯接口做无效调用与刷屏日志）
        self._cap_unavailable: dict[tuple[str, str], tuple[float, str]] = {}
        self._cap_unavailable_ttl = 6 * 3600.0

    # ---------------- 基础 ----------------
    @property
    def available(self) -> bool:
        """传输层是否可用（botpy 客户端是否拿得到）。"""
        return self.transport is not None and getattr(self.transport, "available", True)

    def dry_run(self) -> bool:
        if self._dry_run_getter is None:
            return False
        try:
            return bool(self._dry_run_getter())
        except Exception:  # pragma: no cover - 配置读取失败时按真实执行处理
            return False

    def _bucket_for(self, group_id: str | None) -> TokenBucket | None:
        if not group_id:
            return None
        bucket = self._group_buckets.get(group_id)
        if bucket is None:
            bucket = TokenBucket(self._per_group_qpm, sleep=self._sleep, clock=self._clock)
            self._group_buckets[group_id] = bucket
        return bucket

    def _record_api_call(
        self,
        *,
        group_id: str | None,
        method: str,
        path: str,
        ok: bool,
        err_code: int | None,
        trace_id: str,
        duration_ms: int,
        retries: int,
        caller: str,
        dry_run: bool,
    ) -> None:
        if self.audit is None:
            return
        record = getattr(self.audit, "record_api_call", None)
        if record is None:
            return
        try:
            record(
                group_id=group_id,
                method=method,
                path=path,
                ok=ok,
                err_code=err_code,
                trace_id=trace_id,
                duration_ms=duration_ms,
                retries=retries,
                caller=caller,
                dry_run=dry_run,
            )
        except Exception:  # pragma: no cover - 审计失败不影响业务
            pass

    def _record_capability(
        self,
        group_id: str | None,
        capability: str,
        ok: bool,
        err_code: int | None,
        trace_id: str,
        note: str,
    ) -> None:
        if self.audit is None:
            return
        record = getattr(self.audit, "record_capability", None)
        if record is None:
            return
        key = (str(group_id), capability, bool(ok))
        now = self._clock()
        last = self._cap_log_at.get(key)
        if last is not None and self._cap_dedupe and (now - last) < self._cap_dedupe:
            return  # 同群同能力同结果在去重窗口内只记一次，避免刷屏
        self._cap_log_at[key] = now
        try:
            record(
                group_id=group_id,
                capability=capability,
                ok=ok,
                err_code=err_code,
                trace_id=trace_id,
                note=note,
            )
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _normalize_payload(payload: Any) -> dict[str, Any]:
        """把 botpy 的返回值归一化为 dict。

        botpy 在 content-type 带 charset 时会返回字符串，这里统一解析。
        """
        if payload is None:
            raise QQApiError(
                "接口未返回内容（可能超时或连接重置）", retryable=True, semantic=SEM_TIMEOUT
            )
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"_raw": text}
            return parsed if isinstance(parsed, dict) else {"_data": parsed}
        return {"_data": payload}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        caller: str,
        capability: str | None = None,
        group_id: str | None = None,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        write: bool = False,
    ) -> dict[str, Any]:
        """执行一次 QQ API 调用（含限频、重试、审计、dry-run）。"""
        if not self.available:
            raise QQApiError(
                "QQ 官方接口通道不可用（未找到 botpy 客户端）",
                semantic=SEM_TRANSPORT,
                hint="请确认当前事件来自 qq_official 适配器",
                method=method,
                path=path,
            )

        dry = write and self.dry_run()
        if dry:
            self._record_api_call(
                group_id=group_id,
                method=method,
                path=path,
                ok=True,
                err_code=None,
                trace_id="",
                duration_ms=0,
                retries=0,
                caller=caller,
                dry_run=True,
            )
            return {"_dry_run": True}

        if capability:
            await self._global_bucket.acquire()
            bucket = self._bucket_for(group_id)
            if bucket is not None:
                await bucket.acquire()

        attempt = 0
        started = self._clock()
        last_error: QQApiError | None = None
        while attempt <= self._max_retries:
            try:
                payload = await self.transport.request(  # type: ignore[union-attr]
                    method,
                    path,
                    path_params=path_params,
                    query=query,
                    json_body=json_body,
                )
                data = self._normalize_payload(payload)
                err_code = data.get("err_code")
                if not isinstance(err_code, int) or err_code == 0:
                    # QQ 有时把业务码放在 code 字段（err_code 是另一套编号）
                    nested = data.get("code")
                    if isinstance(nested, int) and nested:
                        err_code = nested
                if isinstance(err_code, int) and err_code != 0:
                    semantic, hint = describe_error(err_code)
                    if semantic == SEM_UNKNOWN:
                        text_semantic, text_hint = semantic_from_message(
                            str(data.get("message") or "")
                        )
                        if text_semantic != SEM_UNKNOWN:
                            semantic, hint = text_semantic, text_hint
                    raise QQApiError(
                        str(data.get("message") or hint),
                        err_code=err_code,
                        semantic=semantic,
                        hint=hint,
                        trace_id=str(data.get("trace_id") or ""),
                        method=method,
                        path=path,
                        retryable=semantic == SEM_SERVER,
                    )
                self._record_api_call(
                    group_id=group_id,
                    method=method,
                    path=path,
                    ok=True,
                    err_code=0,
                    trace_id=str(data.get("trace_id") or ""),
                    duration_ms=int((self._clock() - started) * 1000),
                    retries=attempt,
                    caller=caller,
                    dry_run=False,
                )
                if capability:
                    self._record_capability(group_id, capability, True, 0, "", "")
                return data
            except QQApiError as exc:
                last_error = exc
            except Exception as exc:  # botpy 异常 / aiohttp 异常
                last_error = self._error_from_exception(exc, method, path)
            if last_error is None or not last_error.retryable or attempt >= self._max_retries:
                break
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            await self._sleep(delay)
            attempt += 1

        assert last_error is not None
        self._record_api_call(
            group_id=group_id,
            method=method,
            path=path,
            ok=False,
            err_code=last_error.err_code,
            trace_id=last_error.trace_id,
            duration_ms=int((self._clock() - started) * 1000),
            retries=attempt,
            caller=caller,
            dry_run=False,
        )
        if capability:
            self._record_capability(
                group_id,
                capability,
                False,
                last_error.err_code,
                last_error.trace_id,
                last_error.hint or last_error.message,
            )
        raise last_error

    @staticmethod
    def _error_from_exception(exc: Exception, method: str, path: str) -> QQApiError:
        """把传输层异常翻译成 QQApiError。

        botpy 在非 2xx 时只抛出 message 文案（err_code 丢失），且未知状态码会被
        统一包装成 ServerError —— 因此这里先按文案识别"无权限"类错误并禁止重试，
        避免对着 400 反复重试。
        """
        name = type(exc).__name__
        text_semantic, text_hint = semantic_from_message(str(exc))
        if text_semantic != SEM_UNKNOWN:
            return QQApiError(
                str(exc),
                semantic=text_semantic,
                hint=text_hint,
                method=method,
                path=path,
                retryable=False,
            )
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return QQApiError(
                "请求超时",
                semantic=SEM_TIMEOUT,
                method=method,
                path=path,
                retryable=True,
            )
        if name in {"ServerError", "SequenceNumberError"}:
            status = 429 if name == "SequenceNumberError" else 500
            semantic, hint = STATUS_SEMANTICS[status]
            return QQApiError(
                str(exc),
                semantic=semantic,
                hint=hint,
                http_status=status,
                method=method,
                path=path,
                retryable=True,
            )
        if name in {
            "AuthenticationFailedError",
            "ForbiddenError",
            "NotFoundError",
            "MethodNotAllowedError",
        }:
            status = {
                "AuthenticationFailedError": 401,
                "ForbiddenError": 403,
                "NotFoundError": 404,
                "MethodNotAllowedError": 405,
            }[name]
            semantic, hint = STATUS_SEMANTICS[status]
            return QQApiError(
                str(exc),
                semantic=semantic,
                hint=hint,
                http_status=status,
                method=method,
                path=path,
                retryable=False,
            )
        return QQApiError(
            f"{name}: {exc}",
            semantic=SEM_TRANSPORT,
            method=method,
            path=path,
            retryable=isinstance(exc, OSError),
        )

    # ---------------- 群信息 / 状态 ----------------
    async def get_group_info(self, group_id: str, *, caller: str = "probe") -> GroupProfile:
        """获取群基本信息（白名单能力）。"""
        payload = await self._request(
            "GET",
            "/v2/groups/{group_openid}/info",
            caller=caller,
            capability=CAP_GROUP_INFO,
            group_id=group_id,
            path_params={"group_openid": group_id},
        )
        return GroupProfile.from_api(group_id, payload)

    async def get_bot_state(self, group_id: str, *, caller: str = "probe") -> BotState:
        """获取机器人群内状态（白名单能力）。"""
        payload = await self._request(
            "GET",
            "/v2/groups/{group_openid}/bot_state",
            caller=caller,
            capability=CAP_BOT_STATE,
            group_id=group_id,
            path_params={"group_openid": group_id},
        )
        return BotState(
            group_openid=group_id,
            member_openid=str(payload.get("member_openid") or ""),
            joined_at=parse_iso(payload.get("joined_at")),
            allow_proactive_msg=bool(payload.get("allow_proactive_msg")),
            recv_msg_setting=str(payload.get("recv_msg_setting") or ""),
            member_role=str(payload.get("member_role") or ""),
            fetched_at=now_ts(),
        )

    # ---------------- 入群申请 ----------------
    async def join_request_list(
        self,
        group_id: str,
        *,
        cursor: str = "",
        limit: int = 20,
        caller: str = "join_review",
    ) -> dict[str, Any]:
        """拉取入群申请列表（需群管理员）。"""
        return await self._request(
            "GET",
            "/v2/groups/{group_openid}/join_request_list",
            caller=caller,
            capability=CAP_JOIN_REVIEW,
            group_id=group_id,
            path_params={"group_openid": group_id},
            query={
                "cursor": cursor or "",
                "limit": clamp_int(limit, 20, 1, JOIN_REQUEST_PAGE_MAX),
            },
        )

    async def approve_join_request(
        self,
        group_id: str,
        member_openid: str,
        *,
        op: str,
        join_request_id: str = "",
        reject_reason: str = "",
        add_to_blacklist: bool = False,
        caller: str = "join_review",
    ) -> dict[str, Any]:
        """审批入群申请（approve / decline）。"""
        body: dict[str, Any] = {"op": "approve" if op == "approve" else "decline"}
        if join_request_id:
            body["join_request_id"] = join_request_id
        if body["op"] == "decline":
            if reject_reason:
                body["reject_reason"] = reject_reason[:200]
            if add_to_blacklist:
                body["add_to_member_blacklist"] = True
        return await self._request(
            "POST",
            "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            caller=caller,
            capability=CAP_JOIN_REVIEW,
            group_id=group_id,
            path_params={"group_openid": group_id, "member_openid": member_openid},
            json_body=body,
            write=True,
        )

    # ---------------- 禁言 ----------------
    async def get_restrict_setting(self, group_id: str, *, caller: str = "probe") -> dict[str, Any]:
        """查询群禁言状态（需群管理员）。"""
        return await self._request(
            "GET",
            "/v2/groups/{group_openid}/restrict_chat_setting",
            caller=caller,
            capability=CAP_MUTE,
            group_id=group_id,
            path_params={"group_openid": group_id},
        )

    async def set_member_mutes(
        self,
        group_id: str,
        members: list[dict[str, Any]],
        *,
        caller: str = "moderation",
    ) -> dict[str, Any]:
        """设置成员级禁言（add / update / del，单次 ≤20）。"""
        payload = [dict(item) for item in members][:MUTE_BATCH_MAX]
        return await self._request(
            "POST",
            "/v2/groups/{group_openid}/restrict_chat_setting",
            caller=caller,
            capability=CAP_MUTE,
            group_id=group_id,
            path_params={"group_openid": group_id},
            json_body={"members": payload},
            write=True,
        )

    async def mute_member(
        self,
        group_id: str,
        member_openid: str,
        *,
        seconds: int,
        caller: str = "moderation",
    ) -> dict[str, Any]:
        """禁言成员（op=add），返回原始响应。"""
        until = to_iso(now_ts() + max(1, int(seconds)))
        return await self.set_member_mutes(
            group_id,
            [{"op": "add", "member_openid": member_openid, "mute_expire_at": until}],
            caller=caller,
        )

    async def unmute_member(
        self, group_id: str, member_openid: str, *, caller: str = "moderation"
    ) -> dict[str, Any]:
        """解除成员禁言（op=del）。"""
        return await self.set_member_mutes(
            group_id,
            [{"op": "del", "member_openid": member_openid, "mute_expire_at": ""}],
            caller=caller,
        )

    # ---------------- 消息撤回 ----------------
    async def recall_message(
        self, group_id: str, message_id: str, *, caller: str = "moderation"
    ) -> dict[str, Any]:
        """撤回群消息（需群管理员，且消息发出不超过 2 分钟）。"""
        return await self._request(
            "DELETE",
            "/v2/groups/{group_openid}/messages/{message_id}",
            caller=caller,
            capability=CAP_RECALL,
            group_id=group_id,
            path_params={"group_openid": group_id, "message_id": message_id},
            write=True,
        )

    # ---------------- 成员管理（内邀） ----------------
    async def list_members(
        self,
        group_id: str,
        *,
        cursor: str = "",
        caller: str = "member_admin",
    ) -> dict[str, Any]:
        """获取群成员列表（内邀能力，每次最多 30 条）。"""
        return await self._request(
            "GET",
            "/v2/groups/{group_openid}/members",
            caller=caller,
            capability=CAP_MEMBER_LIST,
            group_id=group_id,
            path_params={"group_openid": group_id},
            query={"cursor": cursor or ""},
        )

    async def get_member(
        self, group_id: str, member_openid: str, *, caller: str = "member_admin"
    ) -> dict[str, Any]:
        """获取群成员详情（内邀能力）。"""
        return await self._request(
            "GET",
            "/v2/groups/{group_openid}/members/{member_openid}",
            caller=caller,
            capability=CAP_MEMBER_LIST,
            group_id=group_id,
            path_params={"group_openid": group_id, "member_openid": member_openid},
        )

    async def batch_remove_members(
        self,
        group_id: str,
        member_openids: list[str],
        *,
        add_to_blacklist: bool = False,
        caller: str = "member_admin",
    ) -> dict[str, Any]:
        """批量移除群成员（内邀能力，单次 ≤20）。"""
        body: dict[str, Any] = {
            "member_openids": [str(item) for item in member_openids][:MUTE_BATCH_MAX]
        }
        if add_to_blacklist:
            body["add_to_member_blacklist"] = True
        return await self._request(
            "POST",
            "/v2/groups/{group_openid}/batch_remove_members",
            caller=caller,
            capability=CAP_REMOVE_MEMBER,
            group_id=group_id,
            path_params={"group_openid": group_id},
            json_body=body,
            write=True,
        )

    async def get_blacklist(
        self,
        group_id: str,
        *,
        cursor: str = "",
        limit: int = 20,
        caller: str = "member_admin",
    ) -> dict[str, Any]:
        """查询群黑名单（内邀能力）。"""
        return await self._request(
            "GET",
            "/v2/groups/{group_openid}/member_blacklist",
            caller=caller,
            capability=CAP_BLACKLIST,
            group_id=group_id,
            path_params={"group_openid": group_id},
            query={
                "cursor": cursor or "",
                "limit": clamp_int(limit, 20, 1, BLACKLIST_PAGE_MAX),
            },
        )

    async def update_blacklist(
        self,
        group_id: str,
        *,
        op: str,
        member_openids: list[str],
        caller: str = "member_admin",
    ) -> dict[str, Any]:
        """群黑名单操作（op=add / del，单次 ≤20；目标在群中时无法拉黑）。"""
        return await self._request(
            "POST",
            "/v2/groups/{group_openid}/member_blacklist",
            caller=caller,
            capability=CAP_BLACKLIST,
            group_id=group_id,
            path_params={"group_openid": group_id},
            json_body={
                "op": "del" if op == "del" else "add",
                "member_openids": [str(item) for item in member_openids][:MUTE_BATCH_MAX],
            },
            write=True,
        )

    # ---------------- 入群自动审批策略（Bot 级，无 group 路径段） ----------------
    STRATEGY_PATH = "/v2/groups/join_approval_strategy"

    async def list_approval_strategies(
        self, *, cursor: str = "", limit: int = 20, caller: str = "policy"
    ) -> dict[str, Any]:
        """查询入群自动审批策略列表。"""
        return await self._request(
            "GET",
            self.STRATEGY_PATH,
            caller=caller,
            query={"cursor": cursor or "", "limit": clamp_int(limit, 20, 1, 50)},
        )

    async def create_approval_strategy(
        self, payload: dict[str, Any], *, caller: str = "policy"
    ) -> dict[str, Any]:
        """创建策略（group_openids 与 group_ids 二选一）。"""
        return await self._request(
            "POST",
            self.STRATEGY_PATH,
            caller=caller,
            json_body=dict(payload),
            write=True,
        )

    async def update_approval_strategy(
        self, strategy_id: str, payload: dict[str, Any], *, caller: str = "policy"
    ) -> dict[str, Any]:
        """修改策略（启用状态 / 过期时间 / 关联群增删 / 备注）。"""
        return await self._request(
            "PATCH",
            self.STRATEGY_PATH + "/{strategy_id}",
            caller=caller,
            path_params={"strategy_id": strategy_id},
            json_body=dict(payload),
            write=True,
        )

    async def delete_approval_strategy(
        self, strategy_id: str, *, caller: str = "policy"
    ) -> dict[str, Any]:
        """删除策略。"""
        return await self._request(
            "DELETE",
            self.STRATEGY_PATH + "/{strategy_id}",
            caller=caller,
            path_params={"strategy_id": strategy_id},
            write=True,
        )

    async def execute_approval_strategy(
        self, strategy_id: str, *, caller: str = "policy"
    ) -> dict[str, Any]:
        """触发策略全量扫描（异步，官方说明约 10 分钟完成）。"""
        return await self._request(
            "POST",
            self.STRATEGY_PATH + "/{strategy_id}/execute",
            caller=caller,
            path_params={"strategy_id": strategy_id},
            json_body={},
            write=True,
        )

    async def update_strategy_whitelist(
        self,
        strategy_id: str,
        *,
        op: str,
        users: list[str],
        caller: str = "policy",
    ) -> dict[str, Any]:
        """批量新增/删除策略白名单号码（单次 ≤10000）。"""
        return await self._request(
            "POST",
            self.STRATEGY_PATH + "/{strategy_id}/whitelist_users",
            caller=caller,
            path_params={"strategy_id": strategy_id},
            json_body={
                "op": "del" if op == "del" else "add",
                "whitelist_users": [str(user) for user in users][:10000],
            },
            write=True,
        )

    # ---------------- 能力探测 ----------------
    async def probe(self, group_id: str, *, caller: str = "probe") -> dict[str, CapabilityResult]:
        """逐项探测该群可用的平台能力。

        探测原则：只调用**只读**接口；写类能力（批量移除成员）没有只读探测接口，
        因此标记 probed=False，表示"只能按需尝试"。
        """
        results: dict[str, CapabilityResult] = {}
        checked = now_ts()

        def recently_unavailable(cap: str) -> bool:
            """近期已知不可用（白名单/权限类）→ 直接复用结论，不再打接口。"""
            item = self._cap_unavailable.get((str(group_id), cap))
            if item is None:
                return False
            until, note = item
            if until <= self._clock():
                self._cap_unavailable.pop((str(group_id), cap), None)
                return False
            results[cap] = CapabilityResult(
                capability=cap,
                ok=False,
                note=note,
                checked_at=checked,
                probed=False,
            )
            return True

        def remember_unavailable(cap: str, exc) -> None:
            if not getattr(exc, "denied", False):
                return
            self._cap_unavailable[(str(group_id), cap)] = (
                self._clock() + self._cap_unavailable_ttl,
                exc.hint or exc.message,
            )

        def ok(cap: str, note: str = "") -> None:
            results[cap] = CapabilityResult(capability=cap, ok=True, note=note, checked_at=checked)

        def fail(cap: str, exc: QQApiError) -> None:
            results[cap] = CapabilityResult(
                capability=cap,
                ok=False,
                err_code=exc.err_code,
                trace_id=exc.trace_id,
                note=exc.hint or exc.message,
                checked_at=checked,
            )

        state: BotState | None = None

        try:
            await self.get_group_info(group_id, caller=caller)
            ok(CAP_GROUP_INFO)
        except QQApiError as exc:
            fail(CAP_GROUP_INFO, exc)

        try:
            state = await self.get_bot_state(group_id, caller=caller)
            ok(CAP_BOT_STATE)
            if state.is_admin:
                ok(CAP_IS_ADMIN, f"群内角色：{state.member_role}")
            else:
                results[CAP_IS_ADMIN] = CapabilityResult(
                    capability=CAP_IS_ADMIN,
                    ok=False,
                    note=f"机器人群内角色为 {state.member_role or '未知'}，需设为管理员",
                    checked_at=checked,
                )
            if state.full_msg:
                ok(CAP_FULL_MSG, "已开启接收全部消息")
            else:
                results[CAP_FULL_MSG] = CapabilityResult(
                    capability=CAP_FULL_MSG,
                    ok=False,
                    note=(
                        f"当前为 {state.recv_msg_setting or '未知'}，"
                        "需在机器人资料页开启「接收全部消息」"
                    ),
                    checked_at=checked,
                )
        except QQApiError as exc:
            fail(CAP_BOT_STATE, exc)
            fail(CAP_IS_ADMIN, exc)
            fail(CAP_FULL_MSG, exc)

        is_admin = state.is_admin if state else False

        if is_admin:
            skip_restrict = recently_unavailable(CAP_MUTE)
            if skip_restrict:                       # 结论同样适用于 recall（同一接口）
                results.setdefault(
                    CAP_RECALL,
                    results[CAP_MUTE],
                )
            try:
                if not skip_restrict:
                    await self.get_restrict_setting(group_id, caller=caller)
                    ok(CAP_MUTE)
                    ok(CAP_RECALL, "群管理员可撤回普通成员消息")
            except QQApiError as exc:
                fail(CAP_MUTE, exc)
                fail(CAP_RECALL, exc)
                remember_unavailable(CAP_MUTE, exc)
            if not recently_unavailable(CAP_JOIN_REVIEW):
                try:
                    await self.join_request_list(group_id, limit=1, caller=caller)
                    ok(CAP_JOIN_REVIEW)
                except QQApiError as exc:
                    fail(CAP_JOIN_REVIEW, exc)
                    remember_unavailable(CAP_JOIN_REVIEW, exc)
        else:
            for cap in (CAP_MUTE, CAP_RECALL, CAP_JOIN_REVIEW):
                results[cap] = CapabilityResult(
                    capability=cap,
                    ok=False,
                    note="需要机器人是群管理员",
                    checked_at=checked,
                )

        if not recently_unavailable(CAP_MEMBER_LIST):
            try:
                await self.list_members(group_id, caller=caller)
                ok(CAP_MEMBER_LIST)
            except QQApiError as exc:
                fail(CAP_MEMBER_LIST, exc)
                remember_unavailable(CAP_MEMBER_LIST, exc)

        if not recently_unavailable(CAP_BLACKLIST):
            try:
                await self.get_blacklist(group_id, limit=1, caller=caller)
                ok(CAP_BLACKLIST)
            except QQApiError as exc:
                fail(CAP_BLACKLIST, exc)
                remember_unavailable(CAP_BLACKLIST, exc)

        results[CAP_REMOVE_MEMBER] = CapabilityResult(
            capability=CAP_REMOVE_MEMBER,
            ok=is_admin,
            note="无只读探测接口，执行时才能确认（需机器人为群管理员）",
            checked_at=checked,
            probed=False,
        )
        return results
