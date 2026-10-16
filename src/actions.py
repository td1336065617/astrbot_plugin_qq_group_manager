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
from datetime import datetime
from .utils import CN_TZ, now_ts, safe_json_dumps, to_iso, truncate

#: AI/显式入口允许的动作（撤回/拉黑/移除**不给** AI，见需求 QG-09）
MANUAL_ACTIONS: tuple[str, ...] = ("mute", "unmute", "warn")

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


def floor_day_key(ts: float | None = None) -> str:
    "地板当日上限的日期键：与插件统一口径（CN_TZ），避免 UTC 主机跨天点错位。"
    moment = datetime.fromtimestamp(float(ts), tz=CN_TZ) if ts is not None else datetime.now(CN_TZ)
    return moment.strftime("%Y-%m-%d")

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
        #: 地板冷却（进程内；重启重置，只用于防刷屏）
        self._floor_seen: dict[tuple[str, str], float] = {}
        #: 地板当日计数（进程内；键为 群x日期）
        self._floor_daily: dict[tuple[str, str], int] = {}
        self._violations: dict[tuple[str, str], list[tuple[float, str]]] = {}
        self.notifier: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self.stats: dict[str, int] = {
            "executed": 0,
            "skipped": 0,
            "failed": 0,
            "downgraded": 0,
        }

    # ------------------------------------------------------------------
    def _seen(
        self,
        group_id: str,
        msg_id: str,
        action: str,
        severity: int = 0,
        *,
        commit: bool = True,
    ) -> bool:
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
        if commit:
            self._done[key] = now
        return False

    def _commit_seen(
        self, group_id: str, msg_id: str, action: str, severity: int = 0
    ) -> None:
        """幂等键落账：只在真的执行成功后调用（dry-run / 失败不落）。"""
        key = (group_id, msg_id or "-", action, int(severity))
        self._done[key] = self._clock()

    def _floor_allowed(
        self,
        group_id: str,
        member_openid: str,
        planned: list[str],
        settings: dict[str, Any],
        *,
        claim: bool = True,
    ) -> str:
        """地板防刷屏：同一成员冷却期内不重复；单群当日不超过上限。

        返回空串=放行；返回 cooldown / daily_limit 表示被抑制。claim=True 时在检查通过的
        同时当场占位：检查与占位之间没有 await，并发进来的第二条会直接看到占位而被抑制
        （否则同一成员的两条消息都能拿到地板动作，冷却形同虚设）。
        """
        if not planned:
            return ""
        cooldown = int(settings.get("review_floor_cooldown_seconds", 0) or 0)
        member_key = (group_id, member_openid or "-")
        if cooldown > 0:
            stamp = self._floor_seen.get(member_key)
            if stamp is not None and self._clock() - stamp < cooldown:
                return "cooldown"
        limit = int(settings.get("review_floor_daily_limit", 0) or 0)
        daily_key = (group_id, floor_day_key())
        if limit > 0 and self._floor_daily.get(daily_key, 0) >= limit:
            return "daily_limit"
        if claim:
            if cooldown > 0:
                self._floor_seen[member_key] = self._clock()
            if limit > 0:
                self._floor_daily[daily_key] = self._floor_daily.get(daily_key, 0) + 1
        return ""


    def _multiplier_for(
        self,
        verdict: Verdict,
        actions: list[str],
        group_id: str,
        sender_openid: str,
        msg_id: str = "",
        floor_driven: bool = False,
    ) -> int:
        """重复违规倍率：地板产生的 review 动作不计（其余与 0.15.2 完全一致）。"""
        if floor_driven or not actions:
            return 1
        return self._repeat_multiplier(group_id, sender_openid, msg_id)

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
        if verdict.stage == "judged_kv" and verdict.is_violation and not hard:
            # KV 兜底无法区分「模型判定」与「用户正文被复述」：宁可只留审计，不产生动作
            return []
        # 动作地板（0.15.3）：judged 的 review 且无硬规则动作时，按配置给最小动作（只允许 warn/report）
        if not hard and verdict.verdict == "review" and verdict.stage == "judged":
            per_category = settings.get("review_floor_by_category")
            override = per_category.get(verdict.category) if isinstance(per_category, dict) else None
            configured_actions = (
                override if override is not None else (settings.get("review_floor_actions") or [])
            )
            floor_actions = [
                action for action in configured_actions if action in ("warn", "report")
            ]
            floor_categories = [str(item) for item in (settings.get("review_floor_categories") or [])]
            if floor_actions and (not floor_categories or verdict.category in floor_categories):
                actions.extend(floor_actions)
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
        stage: str = "judged",
        prompt_version: str = "",
        fallback_index: int = 0,
        sampled: bool = False,
        send: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """记录审核事件并按矩阵执行动作，返回执行摘要。"""
        mode = str(config.mode or settings.get("mode") or "standard")
        actions = self.plan_actions(verdict=verdict, settings=settings, hard_actions=hard_actions)
        planned = list(actions)
        floor_driven = bool(planned) and not hard_actions and verdict.verdict == "review"
        if floor_driven:
            floor_reason = self._floor_allowed(
                group_id, sender_openid, planned, settings, claim=not bool(settings.get("dry_run"))
            )
            if floor_reason:
                floor_driven = False
                actions = [
                    action for action in actions if action not in ("warn", "report")
                ]
                planned = list(actions)
                self.stats["floor_suppressed"] = self.stats.get("floor_suppressed", 0) + 1
                if self.audit is not None:
                    try:  # 被抑制也要留痕：否则事后无法区分「被冷却/日限拦住」与「本来无需动作」
                        self.audit.record_capability(
                            group_id=group_id,
                            capability="review_floor_suppressed",
                            ok=False,
                            err_code=floor_reason,
                        )
                    except Exception:  # pragma: no cover - 留痕失败不影响审核
                        pass
        if mode == "log_only":
            actions = []
        elif mode == "lenient":
            filtered = [action for action in actions if action in LENIENT_ACTIONS]
            # lenient 的语义是"只警告"：矩阵里若只有 recall/mute，也要补一条警告。
            # judged_kv 例外：KV 兜底无法区分模型判定与正文复述，任何模式都不得产生动作。
            if (
                verdict.is_violation
                and verdict.stage != "judged_kv"
                and "warn" not in filtered
            ):
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
                stage=stage,
                prompt_version=prompt_version,
                fallback_index=fallback_index,
                dry_run=self.api.dry_run() if self.api is not None else False,
                sampled=sampled,
                parse_error=verdict.parse_error,
            )

        results: list[ActionResult] = []
        multiplier = (
            self._multiplier_for(verdict, actions, group_id, sender_openid, msg_id, floor_driven)
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

                event_id=event_id,
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
        # 防刷屏按「已尝试」计数（warn 可能因 send 缺失而 ok=False，但那也占了一次提醒）；dry-run 不计
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

    #: AI/显式入口允许的动作（不含 recall/blacklist/remove：撤回/拉黑/踢人不给 AI）
    async def handle_manual(
        self,
        *,
        action: str,
        group_id: str,
        config: GroupConfig,
        settings: dict[str, Any],
        sender_openid: str,
        sender_name: str = "",
        sender_role: str = "member",
        seconds: int | None = None,
        msg_id: str = "",
        umo: str = "",
        dedupe_key: str = "",   # 幂等作用域（AI 工具按「动作+目标」去重）
        source: str = "llm_tool",
        reason: str = "",
    ) -> dict[str, Any]:
        """显式动作入口（AI 工具 / 后台手动）：只允许 MANUAL_ACTIONS。

        与 handle 的区别：不参与矩阵规划、不参与重复倍率、审计固定
        verdict="manual"、source 由调用方给（AI 工具传 llm_tool）。
        """
        want = str(action or "").strip().lower()
        if want not in MANUAL_ACTIONS:
            return {
                "ok": False,
                "error": "动作不在允许清单内",
                "allowed": list(MANUAL_ACTIONS),
            }
        if not sender_openid:
            return {"ok": False, "error": "缺少目标成员 OpenID"}
        mode = str(config.mode or settings.get("mode") or "standard")
        downgraded_to = ""
        if mode == "log_only":
            # 设计 §5.6：仅记录模式是策略结果，不是错误（返回 ok=True + executed=False）
            return {
                "ok": True,
                "executed": False,
                "mode": mode,
                "action": want,
                "message": "本群处于「仅记录」模式：已记录，未实际处置",
            }
        if mode == "lenient" and want == "mute":
            downgraded_to = "warn"
            want = "warn"
        verdict = Verdict(
            verdict="manual",
            category="人工",
            severity=3,
            confidence=1.0,
            reason=reason or "显式处置",
            source=source,
            stage="manual",
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
                verdict="manual",
                category=verdict.category,
                severity=verdict.severity,
                confidence=verdict.confidence,
                reason=verdict.reason,
                rule_hits=safe_json_dumps([]),
                text_digest="",
                text_excerpt=truncate(reason, 120),
                raw_verdict="",
                dry_run=self.api.dry_run() if self.api is not None else False,
                sampled=False,
            )
        result = await self._execute(
            action=want,
            group_id=group_id,
            config=config,
            settings=settings,
            verdict=verdict,
            msg_id=msg_id,
            sender_openid=sender_openid,
            sender_name=sender_name,
            multiplier=1,                      # 显式入口不参与重复倍率
            send=None,
            umo=umo,
            note=reason,
            seconds_override=seconds,
            caller=source,
            ledger_source=source,
            msg_id_override=msg_id or dedupe_key or None,

            event_id=event_id,
        )
        if self.audit is not None:
            self.audit.record_action(
                event_id=event_id,
                group_id=group_id,
                action=result.action,
                target_openid=sender_openid,
                duration_sec=int((result.detail or {}).get("seconds") or 0),
                ok=result.ok,
                dry_run=bool(result.dry_run),
                err_code=result.err_code,
                err_msg=result.message,
                detail=safe_json_dumps(result.detail or {}),
            )
        executed = bool(result.ok) and not bool(result.dry_run)
        policy_skip = bool(downgraded_to) and not executed   # 宽松模式降级但没能真的发送提醒
        return {
            # 设计 §5.6：模式决定"不处置/只提醒"是策略结果，不算能力错误
            "ok": bool(result.ok) or policy_skip,
            "action": result.action,
            "executed": executed,
            "dry_run": bool(result.dry_run),
            "message": result.message,
            "detail": result.detail or {},
            "downgraded_to": downgraded_to,
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
        seconds_override: int | None = None,
        caller: str = "moderation",
        ledger_source: str = "auto",
        msg_id_override: str | None = None,
        event_id: int | None = None,
    ) -> ActionResult:
        """幂等 → 分发 → 按结果决定是否落幂等键。

        幂等键只在"真的执行了"之后落账：dry-run（演练）与失败都不落，
        否则演练一次就会把真实处置在 TTL 内顶掉。
        """
        scope = msg_id_override if msg_id_override is not None else msg_id
        if not scope:
            # 缺省也按目标区分，避免"同群不同人"被误判成重复
            scope = "manual:%s:%s" % (action, sender_openid or "-")
        if self._seen(group_id, scope, action, verdict.severity, commit=False):
            self.stats["skipped"] += 1
            return ActionResult(action=action, ok=True, message="重复推送，已跳过")
        result = await self._dispatch_action(
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
            note=note,
            seconds_override=seconds_override,
            caller=caller,
            ledger_source=ledger_source,

            event_id=event_id,
        )
        if result.ok and not result.dry_run:
            self._commit_seen(group_id, scope, action, verdict.severity)
        return result

    async def _dispatch_action(
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
        seconds_override: int | None = None,
        caller: str = "moderation",
        ledger_source: str = "auto",
        event_id: int | None = None,
    ) -> ActionResult:
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
                    group_id,
                    sender_openid,
                    config,
                    settings,
                    verdict,
                    multiplier,
                    dry_run,
                    seconds_override=seconds_override,
                    caller=caller,
                    ledger_source=ledger_source,

                    event_id=event_id,
                )
            if action == "unmute":
                return await self._unmute(
                    group_id,
                    sender_openid,
                    config,
                    dry_run,
                    caller=caller,
                    ledger_source=ledger_source,
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
        *,
        seconds_override: int | None = None,
        caller: str = "moderation",
        ledger_source: str = "auto",
        event_id: int | None = None,
    ) -> ActionResult:
        if not member_openid:
            return ActionResult(action="mute", ok=False, message="缺少成员 OpenID")
        if not config.capability_ok(CAP_MUTE):
            self.stats["downgraded"] += 1
            return ActionResult(
                action="mute", ok=False, message="能力不可用：禁言需要机器人为群管理员"
            )
        if seconds_override is None:
            seconds = self._mute_seconds(settings, int(verdict.severity or 1), multiplier)
        else:
            max_days = int(settings.get("max_mute_days", 30) or 30)
            ceiling = min(max_days * 86400, MUTE_MAX_SECONDS)
            seconds = max(60, min(int(seconds_override), ceiling))
        response = await self.api.mute_member(
            group_id, member_openid, seconds=seconds, caller=caller
        )
        until_ts = to_iso(now_ts() + seconds)
        if self.audit is not None and not (dry_run or bool(response.get("_dry_run"))):
            await self.audit.upsert_mute(
                group_id=group_id,
                member_openid=member_openid,
                until_unix=now_ts() + seconds,
                reason=f"{verdict.category}（severity={verdict.severity}）",
                source=ledger_source,
                event_id=event_id,
                active=True,
            )
        self.stats["executed"] += 1
        return ActionResult(
            action="mute",
            ok=True,
            dry_run=bool(response.get("_dry_run")),
            detail={"seconds": seconds, "until_ts": until_ts, "multiplier": multiplier},
        )

    async def _unmute(
        self,
        group_id: str,
        member_openid: str,
        config: GroupConfig,
        dry_run: bool,
        *,
        caller: str = "moderation",
        ledger_source: str = "llm_tool",
    ) -> ActionResult:
        """解除禁言（AI/显式入口用；人类「解禁」指令走 main.py 的同名 API）。"""
        if not member_openid:
            return ActionResult(action="unmute", ok=False, message="缺少成员 OpenID")
        if not config.capability_ok(CAP_MUTE):
            self.stats["downgraded"] += 1
            return ActionResult(
                action="unmute", ok=False, message="能力不可用：解禁需要机器人为群管理员"
            )
        response = await self.api.unmute_member(group_id, member_openid, caller=caller)
        if self.audit is not None and not (dry_run or bool(response.get("_dry_run"))):
            await self.audit.set_mute_active(group_id, member_openid, False)
        self.stats["executed"] += 1
        return ActionResult(
            action="unmute",
            ok=True,
            dry_run=bool(response.get("_dry_run")),
            detail={"until_ts": "", "ledger_written": not (dry_run or bool(response.get("_dry_run")))},
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
