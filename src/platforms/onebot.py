"""OneBot v11（aiocqhttp）通道实现。

通过 event.bot.call_action 调用协议端 API；能力不足时显式降级。
入群申请走 request 事件（事件驱动），不再轮询。
"""
from __future__ import annotations

from typing import Any

from ..api_client import QQApiError
from ..models import (
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
    CAPABILITIES,
    BotState,
    CapabilityResult,
    GroupProfile,
)
from ..utils import now_ts

WRITE_ACTIONS = {
    "delete_msg",
    "set_group_ban",
    "set_group_kick",
    "set_group_whole_ban",
    "set_group_add_request",
}


def _int(value: Any) -> Any:
    text = str(value or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text


class OneBotChannel:
    kind = "onebot"

    def __init__(
        self,
        bot: Any,
        platform_id: str = "",
        *,
        self_id: str = "",
        dry_run_getter=None,
        logger=None,
    ) -> None:
        self.bot = bot
        self.platform_id = str(platform_id or "")
        self.self_id = str(self_id or "")
        self._dry_run_getter = dry_run_getter
        self._logger = logger
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._flag_index: dict[str, dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return self.bot is not None

    def dry_run(self) -> bool:
        if self._dry_run_getter is None:
            return False
        try:
            return bool(self._dry_run_getter())
        except Exception:
            return False

    def _self_id(self) -> str:
        if self.self_id:
            return self.self_id
        value = getattr(self.bot, "self_id", "") if self.bot is not None else ""
        return str(value or "")

    async def _call(self, action: str, **params: Any) -> Any:
        if self.bot is None:
            raise QQApiError(
                "OneBot 客户端不可用", semantic="transport_error", hint="协议端未连接"
            )
        if self.dry_run() and action in WRITE_ACTIONS:
            return {"_dry_run": True}
        call = getattr(self.bot, "call_action", None)
        if not callable(call):
            api = getattr(self.bot, "api", None)
            call = getattr(api, "call_action", None)
        if not callable(call):
            raise QQApiError(
                "OneBot 客户端不支持 call_action",
                semantic="unsupported",
                hint="请确认协议端为 OneBot v11",
            )
        try:
            return await call(action, **params)
        except Exception as exc:
            if self._logger is not None:
                self._logger.warning("OneBot %s 调用失败：%s", action, exc)
            raise QQApiError(
                action + " 失败：" + str(exc),
                semantic="transport_error",
                hint="请查看协议端日志",
            ) from exc

    @staticmethod
    def _dry_run_flag(result: Any) -> bool:
        return bool(isinstance(result, dict) and result.get("_dry_run"))

    async def recall_message(
        self, group_id: str, message_id: str, *, caller: str = "moderation"
    ) -> dict[str, Any]:
        result = await self._call("delete_msg", message_id=_int(message_id))
        return {"_dry_run": self._dry_run_flag(result)}

    async def mute_member(
        self, group_id: str, member_openid: str, *, seconds: int, caller: str = "moderation"
    ) -> dict[str, Any]:
        result = await self._call(
            "set_group_ban",
            group_id=_int(group_id),
            user_id=_int(member_openid),
            duration=int(seconds),
        )
        return {"_dry_run": self._dry_run_flag(result)}

    async def unmute_member(
        self, group_id: str, member_openid: str, *, caller: str = "moderation"
    ) -> dict[str, Any]:
        result = await self._call(
            "set_group_ban",
            group_id=_int(group_id),
            user_id=_int(member_openid),
            duration=0,
        )
        return {"_dry_run": self._dry_run_flag(result)}

    async def batch_remove_members(
        self,
        group_id: str,
        member_openids: list,
        *,
        add_to_blacklist: bool = False,
        caller: str = "member_admin",
    ) -> dict[str, Any]:
        dry = False
        for uid in member_openids or []:
            result = await self._call(
                "set_group_kick",
                group_id=_int(group_id),
                user_id=_int(uid),
                reject_add_request=bool(add_to_blacklist),
            )
            dry = dry or self._dry_run_flag(result)
        return {"_dry_run": dry, "remove_members_result": {}}

    async def update_blacklist(
        self, group_id: str, *, op: str, member_openids: list, caller: str = "member_admin"
    ) -> dict[str, Any]:
        if op == "add":
            result = await self.batch_remove_members(
                group_id, member_openids, add_to_blacklist=True, caller=caller
            )
            return {"_dry_run": result.get("_dry_run"), "degraded": True}
        return {"_dry_run": self.dry_run(), "degraded": True}

    async def get_blacklist(
        self, group_id: str, *, limit: int = 20, caller: str = "member_admin"
    ) -> dict[str, Any]:
        return {"users": [], "degraded": True}

    async def list_members(
        self, group_id: str, *, caller: str = "member_admin"
    ) -> dict[str, Any]:
        members = await self._call("get_group_member_list", group_id=_int(group_id))
        return {"members": list(members or [])}

    async def get_member(
        self, group_id: str, member_openid: str, *, caller: str = "member_admin"
    ) -> Any:
        return await self._call(
            "get_group_member_info",
            group_id=_int(group_id),
            user_id=_int(member_openid),
        )

    async def get_group_info(self, group_id: str, *, caller: str = "probe") -> GroupProfile:
        info = await self._call("get_group_info", group_id=_int(group_id))
        payload = dict(info or {}) if isinstance(info, dict) else {}
        if "group_member_num" not in payload and "member_count" in payload:
            payload["group_member_num"] = payload.get("member_count")
        return GroupProfile.from_api(str(group_id), payload)

    async def get_bot_state(self, group_id: str, *, caller: str = "probe") -> BotState:
        self_id = self._self_id()
        role = ""
        if self_id:
            info = await self._call(
                "get_group_member_info", group_id=_int(group_id), user_id=_int(self_id)
            )
            role = str((info or {}).get("role") or "")
        return BotState(
            group_openid=str(group_id),
            member_openid=self_id,
            allow_proactive_msg=True,
            recv_msg_setting="all",
            member_role=role,
            fetched_at=now_ts(),
        )

    async def get_restrict_setting(
        self, group_id: str, *, caller: str = "probe"
    ) -> dict[str, Any]:
        return {"global_rule": {"mode": "none"}, "members": [], "degraded": True}

    def feed_request(self, event: Any) -> dict[str, Any] | None:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return None
        if str(raw.get("post_type") or "") != "request":
            return None
        if str(raw.get("request_type") or "group") not in ("group", ""):
            return None
        flag = str(raw.get("flag") or "")
        group_id = str(raw.get("group_id") or "")
        if not flag or not group_id:
            return None
        sub_type = str(raw.get("sub_type") or "add")
        comment = str(raw.get("comment") or "")
        request = {
            "join_request_id": flag,
            "member_openid": str(raw.get("user_id") or ""),
            "username": str(raw.get("username") or ""),
            "comment": comment,
            "risk_tips": "",
            "verify_info": {
                "method": "onebot",
                "verify_message": comment,
                "review_qa_list": [],
            },
            "apply_source": "invited" if sub_type == "invite" else "self_apply",
            "flag": flag,
            "sub_type": sub_type,
        }
        bucket = self._pending.setdefault(group_id, [])
        if all(item.get("join_request_id") != flag for item in bucket):
            bucket.append(request)
        self._flag_index[flag] = request
        return request

    async def join_request_list(
        self,
        group_id: str,
        *,
        cursor: str = "",
        limit: int = 20,
        caller: str = "join_review",
    ) -> dict[str, Any]:
        key = str(group_id)
        requests = list(self._pending.get(key, []))
        self._pending[key] = []
        return {"list": requests, "next_cursor": ""}

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
        flag = str(join_request_id or "")
        cached = self._flag_index.get(flag) or {}
        sub_type = str(cached.get("sub_type") or "add")
        result = await self._call(
            "set_group_add_request",
            flag=flag,
            sub_type=sub_type,
            approve=(op == "approve"),
            reason=reject_reason or "",
        )
        return {"_dry_run": self._dry_run_flag(result)}

    async def probe(self, group_id: str, *, caller: str = "probe") -> dict[str, Any]:
        results: dict[str, CapabilityResult] = {}

        def mark(cap: str, ok: bool, note: str = "") -> None:
            results[cap] = CapabilityResult(capability=cap, ok=ok, note=note)

        try:
            await self._call("get_group_info", group_id=_int(group_id))
            mark(CAP_GROUP_INFO, True, "OneBot get_group_info")
        except Exception as exc:
            mark(CAP_GROUP_INFO, False, str(exc))

        role = ""
        self_id = self._self_id()
        if self_id:
            try:
                info = await self._call(
                    "get_group_member_info",
                    group_id=_int(group_id),
                    user_id=_int(self_id),
                )
                role = str((info or {}).get("role") or "")
                mark(CAP_BOT_STATE, True, "机器人群内角色 " + (role or "未知"))
            except Exception as exc:
                mark(CAP_BOT_STATE, False, str(exc))
        else:
            mark(CAP_BOT_STATE, False, "无法获取机器人自身 ID")
        is_admin = role in ("owner", "admin")
        mark(CAP_IS_ADMIN, is_admin, "机器人群内角色 " + (role or "未知"))
        mark(CAP_FULL_MSG, True, "OneBot 默认接收群内全部消息")
        mark(CAP_RECALL, is_admin, "OneBot delete_msg")
        mark(CAP_MUTE, is_admin, "OneBot set_group_ban")
        mark(CAP_JOIN_REVIEW, True, "OneBot 加群请求事件")

        try:
            await self._call("get_group_member_list", group_id=_int(group_id))
            mark(CAP_MEMBER_LIST, True, "OneBot get_group_member_list")
        except Exception as exc:
            mark(CAP_MEMBER_LIST, False, str(exc))

        mark(CAP_BLACKLIST, False, "OneBot 无原生黑名单，降级为踢出 + 本地名单")
        mark(CAP_REMOVE_MEMBER, is_admin, "OneBot set_group_kick")

        for cap in CAPABILITIES:
            results.setdefault(cap, CapabilityResult(capability=cap, ok=False, note="未探测"))
        return results

    async def list_approval_strategies(self, *, caller: str = "policy") -> dict[str, Any]:
        return {"strategies": [], "degraded": True}

    async def create_approval_strategy(self, payload: Any, *, caller: str = "policy") -> dict[str, Any]:
        return {"degraded": True}

    async def update_approval_strategy(self, strategy_id: str, payload: Any, *, caller: str = "policy") -> dict[str, Any]:
        return {"degraded": True}

    async def delete_approval_strategy(self, strategy_id: str, *, caller: str = "policy") -> dict[str, Any]:
        return {"degraded": True}

    async def execute_approval_strategy(self, strategy_id: str, *, caller: str = "policy") -> dict[str, Any]:
        return {"degraded": True}

    async def update_strategy_whitelist(self, strategy_id: str, *, op: str, users: list, caller: str = "policy") -> dict[str, Any]:
        return {"degraded": True}
