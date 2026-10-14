"""处置执行器：把判定结果落到实际动作，并写入审计与台账。

动作与降级规则见 docs/设计方案.md §6：
- 处置矩阵 verdict × severity → 动作列表（WebUI 可编辑）；
- 模式约束：log_only 不执行任何动作，lenient 只允许 warn / report；
- 能力约束：撤回/禁言需要机器人是群管理员，拉黑/移除属于内邀能力，
  能力不可用时降级为「警告 + 上报」，并在审计里留下原因；
- dry-run：写操作在 API 客户端层被拦截，这里如实记录 dry_run 标记；
- 幂等：同一 (群, 消息, 动作) 在 TTL 内只执行一次，避免平台重复推送导致重复处置。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from .models import (
    CAP_BLACKLIST,
    CAP_MUTE,
    CAP_RECALL,
    CAP_REMOVE_MEMBER,
    MUTE_MAX_SECONDS,
    ActionResult,
    GroupConfig,
    Verdict,
)
from .utils import now_ts, safe_json_dumps, to_iso, truncate

MATRIX_ACTIONS: tuple[str, ...] = (
    "warn",
    "recall",
    "mute",
    "report",
    "blacklist",
    "remove",
)

#: lenient 模式允许的动作
LENIENT_ACTIONS = {"warn", "report"}

IDEMPOTENT_TTL = 3600.0
REPEAT_WINDOW = 86400.0
REPEAT_MAX_MULTIPLIER = 3

WARN_TEMPLATE = (
    "提醒：{name} 的消息未通过群内容审核（{category}）。请遵守群规，违规内容会被撤回或禁言。"
)


class ActionExecutor:
    """执行处置动作并写审计。"""

    def __init__(
        self,
        *,
        api: Any,
        store: Any,
        audit: Any = None,
        logger: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.store = store
        self.audit = audit
        self.logger = logger
        self._clock = clock
        self._done: dict[tuple[str, str, str, int], float] = {}
        self._violations: dict[tuple[str, str], list[tuple[float, str]]] = {}
        self.notifier: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self.stats: dict[str, int] = {
            "executed": 0,
            "skipped": 0,
            "failed": 0,
            "downgraded": 0,
        }

    # ------------------------------------------------------------------
    def _seen(self, group_id: str, msg_id: str, action: str, severity: int = 0) -> bool:
        """幂等判断：近期已对「同一条消息 + 同一个动作 + 同一个严重度」执行过则返回 True。

        严重度必须进入幂等键：同一条消息可能先被刷屏链路按 3 级处置、再被内容链路判到 4 级，
        若只按 (群, 消息, 动作) 去重，更重的 4 级禁言会被"重复推送"顶掉，等于处罚被削弱。
        """
        key = (group_id, msg_id or "-", action, int(severity))
        now = self._clock()
        if len(self._done) > 4000:
            self._done = {
                item: stamp for item, stamp in self._done.items() if now - stamp < IDEMPOTENT_TTL
            }
        stamp = self._done.get(key)
        if stamp is not None and now - stamp < IDEMPOTENT_TTL:
            return True
        self._done[key] = now
        return False

    def _repeat_multiplier(self, group_id: str, member_openid: str, msg_id: str = "") -> int:
        """24 小时内重复违规次数 → 时长倍数（1~3）。

        同一条消息只计一次：一条消息可能同时被刷屏链路与内容链路处置，
        计两次会让后续禁言时长被更快放大（对群成员不公平）。
        """
        if not member_openid:
            return 1
        key = (group_id, member_openid)
        now = self._clock()
        bucket = [
            item for item in self._violations.get(key, []) if now - item[0] < REPEAT_WINDOW
        ]
        if msg_id and any(item[1] == msg_id for item in bucket):
            self._violations[key] = bucket
            return max(1, min(REPEAT_MAX_MULTIPLIER, len(bucket)))
        bucket.append((now, msg_id))
        self._violations[key] = bucket
        return max(1, min(REPEAT_MAX_MULTIPLIER, len(bucket)))

    # ------------------------------------------------------------------
    def plan_actions(
        self,
        *,
        verdict: Verdict,
        settings: dict[str, Any],
        hard_actions: list[str] | None = None,
    ) -> list[str]:
        """计算本次要执行的动作列表。"""
        actions: list[str] = []
        hard = [action for action in (hard_actions or []) if action in MATRIX_ACTIONS]
        if hard:
            actions.extend(hard)
            if verdict.verdict == "review" and "report" not in actions:
                actions.append("report")
        elif verdict.is_violation:
            matrix = settings.get("action_matrix") or {}
            bucket = matrix.get("violation") or {}
            severity = str(max(1, min(5, int(verdict.severity or 1))))
            configured = bucket.get(severity) or bucket.get(str(severity)) or []
            if isinstance(configured, str):
                configured = [configured]
            actions.extend(
                action
                for action in configured
                if isinstance(action, str) and action in MATRIX_ACTIONS
            )
        return actions

    def _mute_seconds(self, settings: dict[str, Any], severity: int, multiplier: int) -> int:
        steps = settings.get("mute_steps") or {}
        raw = steps.get(str(severity), 600) if isinstance(steps, dict) else 600
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            seconds = 600
        if settings.get("repeat_offense_multiplier", True):
            seconds *= max(1, multiplier)
        cap = int(settings.get("max_mute_days", 30) or 30) * 86400
        return max(1, min(seconds, min(cap, MUTE_MAX_SECONDS)))

    # ------------------------------------------------------------------
    async def handle(
        self,
        *,
        group_id: str,
        config: GroupConfig,
        verdict: Verdict,
        settings: dict[str, Any],
        msg_id: str = "",
        sender_openid: str = "",
        sender_name: str = "",
        sender_role: str = "member",
        message_excerpt: str = "",
        text_digest: str = "",
        rule_hits: list[dict[str, Any]] | None = None,
        hard_actions: list[str] | None = None,
        source: str = "llm",
        umo: str = "",
        provider_id: str = "",
        latency_ms: int = 0,
        sampled: bool = False,
        send: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """记录审核事件并按矩阵执行动作，返回执行摘要。"""
        mode = str(config.mode or settings.get("mode") or "standard")
        actions = self.plan_actions(verdict=verdict, settings=settings, hard_actions=hard_actions)
        planned = list(actions)
        if mode == "log_only":
            actions = []
        elif mode == "lenient":
            filtered = [action for action in actions if action in LENIENT_ACTIONS]
            # lenient 的语义是"只警告"：矩阵里若只有 recall/mute，也要补一条警告，
            # 否则成员完全收不到反馈。
            if verdict.is_violation and "warn" not in filtered:
                filtered.insert(0, "warn")
            actions = filtered
        if verdict.verdict == "allow":
            actions = []

        dry_run_now = bool(self.api.dry_run()) if self.api is not None else True
        destructive_planned = [
            action for action in planned if action in ("recall", "mute", "blacklist", "remove")
        ]
        capability_ok = True
        if "recall" in destructive_planned and not config.capability_ok(CAP_RECALL):
            capability_ok = False
        if "mute" in destructive_planned and not config.capability_ok(CAP_MUTE):
            capability_ok = False
        # 违规但破坏性动作被 dry-run / 能力限制拦下时，至少要有一条警告，
        # 否则群内完全看不到反馈（使用者会以为插件没工作）。
        if (
            verdict.is_violation
            and "warn" not in actions
            and settings.get("dry_run_warn", True)
            and destructive_planned
            and (dry_run_now or not capability_ok)
        ):
            actions.insert(0, "warn")

        # 把"被判违规但动作被模式/dry-run 拦下"这件事写清楚，避免使用者以为插件没工作
        limited = [action for action in planned if action not in actions]
        notes: list[str] = []
        if mode == "lenient":
            notes.append("宽松模式只警告不撤回/禁言")
        elif mode == "log_only":
            notes.append("仅记录模式不执行任何处置")
        if dry_run_now:
            notes.append("dry-run 只警告、不实际处置")
        limited_note = ("（" + "；".join(notes) + "）") if notes else ""
        if limited and self.logger is not None:
            self.logger.info(
                "以下动作未执行：%s（模式=%s%s，dry_run=%s）",
                limited,
                mode,
                "，群级覆盖" if str(config.mode or "") else "，跟随全局",
                dry_run_now,
            )

        event_id: int | None = None
        if self.audit is not None:
            event_id = await self.audit.insert_event(
                group_id=group_id,
                group_name=config.name,
                msg_id=msg_id,
                sender_openid=sender_openid,
                sender_name=sender_name,
                sender_role=sender_role,
                source=source,
                verdict=verdict.verdict,
                category=verdict.category,
                severity=verdict.severity,
                confidence=verdict.confidence,
                reason=verdict.reason,
                rule_hits=safe_json_dumps(rule_hits or []),
                text_digest=text_digest,
                text_excerpt=truncate(message_excerpt, 120),
                raw_verdict=verdict.raw,
                latency_ms=latency_ms,
                provider_id=provider_id,
                dry_run=self.api.dry_run() if self.api is not None else False,
                sampled=sampled,
                parse_error=verdict.parse_error,
            )

        results: list[ActionResult] = []
        multiplier = (
            self._repeat_multiplier(group_id, sender_openid, msg_id) if actions else 1
        )
        for action in actions:
            result = await self._execute(
                action=action,
                group_id=group_id,
                config=config,
                settings=settings,
                verdict=verdict,
                msg_id=msg_id,
                sender_openid=sender_openid,
                sender_name=sender_name,
                multiplier=multiplier,
                send=send,
                umo=umo,
                note=limited_note,
            )
            results.append(result)
            if self.audit is not None:
                self.audit.record_action(
                    event_id=event_id,
                    group_id=group_id,
                    action=result.action,
                    target_openid=sender_openid,
                    duration_sec=int(result.detail.get("seconds") or 0),
                    until_ts=str(result.detail.get("until_ts") or ""),
                    ok=result.ok,
                    dry_run=result.dry_run,
                    err_code=result.err_code,
                    err_msg=result.message,
                    trace_id=str(result.detail.get("trace_id") or ""),
                    detail=safe_json_dumps(result.detail),
                )
        return {
            "event_id": event_id,
            "mode": mode,
            "planned": planned,
            "skipped": limited,
            "actions": [result.to_dict() for result in results],
            "verdict": verdict.verdict,
            "category": verdict.category,
            "severity": verdict.severity,
        }

    async def _execute(
        self,
        *,
        action: str,
        group_id: str,
        config: GroupConfig,
        settings: dict[str, Any],
        verdict: Verdict,
        msg_id: str,
        sender_openid: str,
        sender_name: str,
        multiplier: int,
        send: Callable[[str], Awaitable[None]] | None,
        umo: str,
        note: str = "",
    ) -> ActionResult:
        if self._seen(group_id, msg_id, action, verdict.severity):
            self.stats["skipped"] += 1
            return ActionResult(action=action, ok=True, message="重复推送，已跳过")
        dry_run = bool(self.api.dry_run()) if self.api is not None else True
        # dry-run 只拦截"破坏性动作"（撤回/禁言/拉黑/移除）；警告与上报是给用户/管理员的
        # 反馈，默认照常执行，否则 dry-run 期间使用者会误以为插件没工作。
        dry_run_warn = bool(settings.get("dry_run_warn", True))

        try:
            if action == "warn":
                return await self._warn(
                    send=send,
                    group_id=group_id,
                    sender_name=sender_name,
                    verdict=verdict,
                    dry_run=dry_run and not dry_run_warn,
                    note=note,
                )
            if action == "recall":
                return await self._recall(group_id, msg_id, config, dry_run)
            if action == "mute":
                return await self._mute(
                    group_id, sender_openid, config, settings, verdict, multiplier, dry_run
                )
            if action == "blacklist":
                return await self._blacklist(group_id, sender_openid, config, dry_run)
            if action == "remove":
                return await self._remove(group_id, sender_openid, config, settings, dry_run)
            if action == "report":
                return await self._report(
                    group_id=group_id,
                    config=config,
                    verdict=verdict,
                    sender_name=sender_name,
                    sender_openid=sender_openid,
                    msg_id=msg_id,
                    umo=umo,
                    dry_run=dry_run,
                    dry_run_warn=dry_run_warn,
                )
        except Exception as exc:  # pragma: no cover - 单个动作失败不影响其它动作
            self.stats["failed"] += 1
            if self.logger is not None:
                self.logger.warning("处置动作 %s 执行异常：%s", action, exc)
            return ActionResult(action=action, ok=False, message=str(exc))
        return ActionResult(action=action, ok=False, message="未知动作")

    # -- 各动作实现 ----------------------------------------------------
    async def _warn(
        self,
        *,
        send: Callable[[str], Awaitable[None]] | None,
        group_id: str,
        sender_name: str,
        verdict: Verdict,
        dry_run: bool,
        note: str = "",
    ) -> ActionResult:
        if send is None:
            self.stats["skipped"] += 1
            return ActionResult(
                action="warn", ok=False, message="当前上下文无法发送消息（可能已过被动回复窗口）"
            )
        text = WARN_TEMPLATE.format(
            name=sender_name or "该成员", category=verdict.category or "其他"
        )
        if note:
            text += note
        if dry_run:
            self.stats["executed"] += 1
            return ActionResult(action="warn", ok=True, dry_run=True, message="dry-run：未实际发送")
        await send(text)
        self.stats["executed"] += 1
        return ActionResult(action="warn", ok=True, detail={"group_id": group_id})

    async def _recall(
        self, group_id: str, msg_id: str, config: GroupConfig, dry_run: bool
    ) -> ActionResult:
        if not msg_id:
            return ActionResult(action="recall", ok=False, message="缺少消息 ID")
        if not config.capability_ok(CAP_RECALL):
            self.stats["downgraded"] += 1
            return ActionResult(
                action="recall", ok=False, message="能力不可用：撤回需要机器人为群管理员"
            )
        response = await self.api.recall_message(group_id, msg_id, caller="moderation")
        self.stats["executed"] += 1
        return ActionResult(
            action="recall",
            ok=True,
            dry_run=bool(response.get("_dry_run")),
            detail={"trace_id": str(response.get("trace_id") or "")},
        )

    async def _mute(
        self,
        group_id: str,
        member_openid: str,
        config: GroupConfig,
        settings: dict[str, Any],
        verdict: Verdict,
        multiplier: int,
        dry_run: bool,
    ) -> ActionResult:
        if not member_openid:
            return ActionResult(action="mute", ok=False, message="缺少成员 OpenID")
        if not config.capability_ok(CAP_MUTE):
            self.stats["downgraded"] += 1
            return ActionResult(
                action="mute", ok=False, message="能力不可用：禁言需要机器人为群管理员"
            )
        seconds = self._mute_seconds(settings, int(verdict.severity or 1), multiplier)
        response = await self.api.mute_member(
            group_id, member_openid, seconds=seconds, caller="moderation"
        )
        until_ts = to_iso(now_ts() + seconds)
        if self.audit is not None:
            await self.audit.upsert_mute(
                group_id=group_id,
                member_openid=member_openid,
                until_unix=now_ts() + seconds,
                reason=f"{verdict.category}（severity={verdict.severity}）",
                source="auto",
                active=True,
            )
        self.stats["executed"] += 1
        return ActionResult(
            action="mute",
            ok=True,
            dry_run=bool(response.get("_dry_run")),
            detail={"seconds": seconds, "until_ts": until_ts, "multiplier": multiplier},
        )

    async def _blacklist(
        self, group_id: str, member_openid: str, config: GroupConfig, dry_run: bool
    ) -> ActionResult:
        if not member_openid:
            return ActionResult(action="blacklist", ok=False, message="缺少成员 OpenID")
        if not config.capability_ok(CAP_BLACKLIST):
            members = self.store.local_blacklist(group_id)
            if member_openid not in members:
                members.append(member_openid)
                await self.store.update_local_blacklist(group_id, members)
            self.stats["downgraded"] += 1
            return ActionResult(
                action="blacklist", ok=True, message="平台黑名单不可用，已写入本地黑名单"
            )
        response = await self.api.update_blacklist(
            group_id, op="add", member_openids=[member_openid], caller="moderation"
        )
        self.stats["executed"] += 1
        return ActionResult(
            action="blacklist",
            ok=True,
            dry_run=bool(response.get("_dry_run")),
            detail={"fail_openids": response.get("fail_openids") or []},
        )

    async def _remove(
        self,
        group_id: str,
        member_openid: str,
        config: GroupConfig,
        settings: dict[str, Any],
        dry_run: bool,
    ) -> ActionResult:
        if not settings.get("auto_remove", False):
            self.stats["downgraded"] += 1
            return ActionResult(action="remove", ok=False, message="未开启自动移除成员（默认关闭）")
        if not config.capability_ok(CAP_REMOVE_MEMBER):
            self.stats["downgraded"] += 1
            return ActionResult(
                action="remove", ok=False, message="能力不可用：批量移除属于内邀能力"
            )
        response = await self.api.batch_remove_members(
            group_id, [member_openid], add_to_blacklist=False, caller="moderation"
        )
        self.stats["executed"] += 1
        return ActionResult(
            action="remove",
            ok=True,
            dry_run=bool(response.get("_dry_run")),
            detail={"result": response.get("remove_members_result")},
        )

    async def _report(
        self,
        *,
        group_id: str,
        config: GroupConfig,
        verdict: Verdict,
        sender_name: str,
        sender_openid: str,
        msg_id: str,
        umo: str,
        dry_run: bool,
        dry_run_warn: bool = True,
    ) -> ActionResult:
        payload = {
            "group_id": group_id,
            "group_name": config.name,
            "verdict": verdict.verdict,
            "category": verdict.category,
            "severity": verdict.severity,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "sender_name": sender_name,
            "sender_openid": sender_openid,
            "msg_id": msg_id,
            "umo": umo,
        }
        if self.notifier is None:
            self.stats["skipped"] += 1
            return ActionResult(action="report", ok=True, message="未配置通知会话，仅记录审计")
        if dry_run and not dry_run_warn:
            self.stats["executed"] += 1
            return ActionResult(
                action="report", ok=True, dry_run=True, message="dry-run：未实际通知"
            )
        await self.notifier("moderation", payload)
        self.stats["executed"] += 1
        return ActionResult(action="report", ok=True)

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return dict(self.stats)
