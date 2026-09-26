"""入群申请轮询与审批引擎。

为什么是轮询：QQ 的 GROUP_JOIN_REQUEST 属于 intent 1<<24，AstrBot 的 qq_official
适配器既未订阅也未实现回调，事件收不到（见 docs/平台能力调研.md §1）。因此本模块
按固定间隔调用 /v2/groups/{gid}/join_request_list，并用数据库去重保证幂等。

决策链（docs/设计方案.md §4.2）：
    risk_tips / bot / 黑名单等硬规则 → LLM 审核申请人 → 自动审批或转人工
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .moderator import extract_json_object
from .utils import clamp_float, now_ts, safe_json_dumps, truncate

#: 只有这些通道可能提供账号维度画像；官方/未知平台「没有数据源」不等于「资料缺失」
PROFILE_CAPABLE_KINDS = ("onebot",)

JOIN_SYSTEM_PROMPT = """你是 QQ 群入群申请审核引擎。根据群规与申请信息，判断是否放行该申请人。
只输出一个 JSON 对象，不要输出解释性文字，不要使用 Markdown 代码块。

字段：
- decision: "approve" | "decline"
- confidence: 0-1 的小数
- risk: "无" | "广告" | "骚扰" | "诈骗" | "违规内容" | "其他"
- reason: 不超过 40 字的中文理由（拒绝时会给申请人看，请中性客观）

准则：
- 信息正常、无风险信号 → approve
- 昵称/验证消息含广告、引流、联系方式、诈骗话术 → decline
- 验证问题答案与问题明显无关或答非所问 → decline
- 若给出【入群答案要求】，答案明显不符合要求 → decline（这是群主设定的硬性门槛）
- 信息不足、无法判断 → decline 并把 confidence 给低值（交由人工处理）
- 不臆测：不要因为昵称特殊字符、地区等无关特征拒绝正常用户
- 账号年龄过短、QQ 等级过低、资料空白只作为风险加分，不得单独定罪
- 「画像缺失」是协议端能力问题，不等于申请人可疑"""

AVATAR_SYSTEM_PROMPT = """你是 QQ 群入群申请的头像审核器。只根据头像图片判断该账号是否属于广告/引流/色情。
只输出一个 JSON 对象，不要输出解释性文字，不要使用 Markdown 代码块。

字段：
- risk: true | false
- confidence: 0-1 的小数
- reason: 不超过 30 字的中文理由

准则：
- 正常人物、风景、动漫、表情包、纯色或默认头像 → risk=false
- 明显色情/性暗示/裸露画面、二维码、加群或联系方式水印、广告版式 → risk=true
- 拿不准 → risk=false 且 confidence 给低值（不要臆测）"""

AVATAR_USER_TEMPLATE = """【申请人昵称】{username}
【QQ等级】{qq_level}
请判断该申请人的头像是否属于需要拒绝入群的风险头像。"""

JOIN_USER_TEMPLATE = """【群规摘要】{rules_brief}
【申请来源】{apply_source}
【申请人昵称】{username}
【申请人画像】QQ等级={qq_level}｜账号年龄={account_age_days}天｜注册时间={reg_time}｜QID={qid}｜性别={sex}
【画像完整度】{profile_state}
【验证方式】{verify_method}
【验证消息】
<<<MESSAGE
{verify_message}
MESSAGE>>>
【问答】
{review_qa}
【入群答案要求】{answer_expectation}
【平台风险提示】{risk_tips}
【是否机器人账号】{is_bot}
【邀请人 OpenID】{invited_by}"""


@dataclass(slots=True)
class JoinDecision:
    """一次入群申请的判定结果。"""

    op: str = "decline"
    auto: bool = False
    confidence: float = 0.0
    reason: str = ""
    risk: str = "无"
    blacklist: bool = False
    source: str = "rule"
    #: 命中的门槛名（account_age / qq_level / qid / profile_missing / avatar）
    gate: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "auto": self.auto,
            "confidence": self.confidence,
            "reason": self.reason,
            "risk": self.risk,
            "blacklist": self.blacklist,
            "source": self.source,
            "gate": self.gate,
        }


@dataclass
class JoinStats:
    """运行统计。"""

    polls: int = 0
    fetched: int = 0
    approved: int = 0
    declined: int = 0
    manual: int = 0
    failed: int = 0
    last_error: str = ""
    last_poll: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "fetched": self.fetched,
            "approved": self.approved,
            "declined": self.declined,
            "manual": self.manual,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_poll": self.last_poll,
        }


class JoinReviewer:
    """入群申请轮询 + 判定 + 审批。"""

    def __init__(
        self,
        *,
        api: Any,
        store: Any,
        audit: Any = None,
        profile_getter: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        judge_call: Callable[..., Awaitable[str]] | None = None,
        notifier: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        logger: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.store = store
        self.audit = audit
        self.profile_getter = profile_getter
        self.judge_call = judge_call
        self._judge_signature_for: Any = None
        self._judge_kwargs: set[str] | None = set()
        self.notifier = notifier
        self.logger = logger
        self._clock = clock
        self.stats = JoinStats()
        self._seen: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    async def poll_all(self) -> dict[str, int]:
        """按群轮询入群申请；返回 {group_id: 新增待审数量}。"""
        result: dict[str, int] = {}
        for group_id in list(self.store.groups()):
            try:
                result[group_id] = len(await self.poll_group(group_id))
            except Exception as exc:  # pragma: no cover - 单群失败不影响其它群
                self.stats.failed += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                if self.logger is not None:
                    self.logger.warning("轮询群 %s 的入群申请失败：%s", group_id, exc)
        self.stats.polls += 1
        self.stats.last_poll = now_ts()
        return result

    async def poll_group(self, group_id: str) -> list[dict[str, Any]]:
        """拉取一页入群申请并处理，返回本次新登记（待审）的申请。"""
        cursor = await self.store.get_join_cursor(group_id)
        response = await self.api.join_request_list(
            group_id, cursor=cursor, limit=20, caller="join_review"
        )
        next_cursor = str(response.get("next_cursor") or "")
        await self.store.set_join_cursor(group_id, next_cursor)
        requests = [item for item in (response.get("list") or []) if isinstance(item, dict)]
        self.stats.fetched += len(requests)

        created: list[dict[str, Any]] = []
        for request in requests:
            request_id = str(request.get("join_request_id") or "")
            if not request_id or await self._already_handled(request_id):
                continue
            await self._enrich_profile(group_id, request)
            mode = self._mode_for(group_id)
            decision = await self.judge(group_id, request, mode=mode)
            if decision.auto and mode != "human":
                await self.submit(group_id, request, decision, by=decision.source)
            else:
                self._pending[request_id] = {
                    "group_id": group_id,
                    "request": request,
                    "decision": decision.to_dict(),
                    "created_at": now_ts(),
                }
                self.stats.manual += 1
                await self._persist(group_id, request, decision, decided_by="pending")
                created.append(self._pending[request_id])
                await self._notify_pending(group_id, request, decision)
        return created

    # ------------------------------------------------------------------
    def _mode_for(self, group_id: str) -> str:
        config = self.store.group(group_id)
        mode = str((config.join_review_mode if config else "") or "")
        return mode or str(self.store.get_setting("join_review_mode") or "off")

    async def _enrich_profile(self, group_id: str, request: dict[str, Any]) -> None:
        """采集申请人画像并挂到 request 上（失败一律降级，不阻塞审批）。"""
        if self.profile_getter is None or request.get("profile"):
            return
        try:
            request["profile"] = await self.profile_getter(group_id, request)
        except Exception as exc:  # pragma: no cover - 双保险
            if self.logger is not None:
                self.logger.debug("画像采集失败：%s", exc)
            request["profile"] = {
                "degraded": True,
                "failed": True,
                "note": f"画像采集异常：{type(exc).__name__}",
            }

    @staticmethod
    def _answer_expectation(settings: dict[str, Any]) -> str:
        """把入群答案要求渲染成给 LLM 看的一句话（未配置时返回“（无特殊要求）”）。"""
        parts: list[str] = []
        expected = str(settings.get("join_expected_answer") or "").strip()
        if expected:
            parts.append(f"答案须包含「{expected}」")
        keywords = [
            str(item).strip()
            for item in (settings.get("join_answer_keywords") or [])
            if str(item).strip()
        ]
        if keywords:
            parts.append("答案须包含其中之一：" + "、".join(keywords))
        pattern = str(settings.get("join_answer_regex") or "").strip()
        if pattern:
            parts.append(f"答案须匹配正则 /{pattern}/")
        return "；".join(parts) or "（无特殊要求）"

    @staticmethod
    def _answer_gate(
        verify: dict[str, Any], settings: dict[str, Any]
    ) -> JoinDecision | None:
        """入群答案校验：三项规则全空则不校验（默认与旧版行为一致）。

        申请人答案的来源：verify_info.review_qa_list[].answer（QQ 群设置的验证问题）
        与 verify_info.verify_message（自定义验证消息）。
        """
        expected = str(settings.get("join_expected_answer") or "").strip()
        keywords = [
            str(item).strip()
            for item in (settings.get("join_answer_keywords") or [])
            if str(item).strip()
        ]
        pattern = str(settings.get("join_answer_regex") or "").strip()
        if not (expected or keywords or pattern):
            return None

        answers = [
            str(item.get("answer") or "")
            for item in (verify.get("review_qa_list") or [])
            if isinstance(item, dict)
        ]
        answers.append(str(verify.get("verify_message") or ""))
        joined = "\n".join(answer for answer in answers if answer).strip()
        case_sensitive = bool(settings.get("join_answer_case_sensitive", False))
        haystack = joined if case_sensitive else joined.casefold()

        failures: list[str] = []
        if expected:
            needle = expected if case_sensitive else expected.casefold()
            if needle not in haystack:
                failures.append("未包含期望答案")
        if keywords:
            pool = keywords if case_sensitive else [item.casefold() for item in keywords]
            if not any(item in haystack for item in pool):
                failures.append("未包含任一关键词")
        if pattern:
            try:
                if not re.search(pattern, joined, 0 if case_sensitive else re.I):
                    failures.append("正则未匹配")
            except re.error:
                # 正则写错不拦人：按未配置处理，避免误拒
                pass
        if not failures:
            return None

        detail = "、".join(failures)
        action = str(settings.get("join_answer_action") or "manual")
        if action == "pass":
            return JoinDecision(
                op="approve",
                auto=True,
                confidence=0.6,
                reason=f"入群答案校验未通过（{detail}），配置为放行",
                source="rule",
                gate="answer",
            )
        if action == "decline":
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=0.75,
                reason=f"入群答案不符合要求：{detail}",
                risk="其他",
                source="rule",
                gate="answer",
            )
        return JoinDecision(
            op="approve",
            auto=False,
            reason=f"入群答案校验未通过（{detail}），转人工复核",
            source="manual",
            gate="answer",
        )

    @staticmethod
    def _profile_state(profile: dict[str, Any]) -> str:
        if not profile:
            return "未采集"
        if str(profile.get("kind") or "") not in PROFILE_CAPABLE_KINDS:
            return "本通道不提供（官方/未知平台）"
        if profile.get("failed"):
            return "采集失败：" + str(profile.get("note") or "")
        if profile.get("degraded"):
            return "部分缺失：" + str(profile.get("note") or "")
        return "完整"

    def _profile_gate(self, profile: dict[str, Any], settings: dict[str, Any]) -> JoinDecision | None:
        """资料缺失策略 + 画像硬规则；不适用时返回 None（继续原链路）。"""
        if not isinstance(profile, dict) or not profile:
            return None
        # 官方/未知平台没有数据源，「缺失」无从谈起 —— 保持旧行为
        if str(profile.get("kind") or "") not in PROFILE_CAPABLE_KINDS:
            return None

        if profile.get("degraded") or profile.get("failed"):
            mode = str(settings.get("join_profile_missing") or "manual")
            if mode == "pass":
                return None
            if mode == "decline":
                return JoinDecision(
                    op="decline",
                    auto=True,
                    confidence=1.0,
                    reason="无法获取申请人资料（按策略拒绝）",
                    risk="其他",
                    source="rule",
                    gate="profile_missing",
                )
            return JoinDecision(
                op="approve",
                auto=False,
                reason="资料缺失，转人工复核",
                source="manual",
                gate="profile_missing",
            )

        action = str(settings.get("join_gate_action") or "decline")
        blacklist = bool(settings.get("join_decline_blacklist", True))
        limit_days = int(settings.get("join_min_account_days", 0) or 0)
        age = profile.get("account_age_days")
        if limit_days > 0 and isinstance(age, int) and age < limit_days:
            return self._gate_decision(
                action, f"账号注册仅 {age} 天（< {limit_days} 天）", "account_age", blacklist
            )
        limit_level = int(settings.get("join_min_qq_level", 0) or 0)
        level = profile.get("qq_level")
        if limit_level > 0 and isinstance(level, int) and level < limit_level:
            return self._gate_decision(
                action, f"QQ 等级 {level}（< {limit_level}）", "qq_level", blacklist
            )
        if settings.get("join_require_qid") and not str(profile.get("qid") or "").strip():
            return self._gate_decision(action, "缺少 QID", "qid", blacklist)
        return None

    @staticmethod
    def _gate_decision(
        action: str, reason: str, gate: str, blacklist: bool = False
    ) -> JoinDecision | None:
        if action == "pass":
            return None
        if action == "manual":
            return JoinDecision(
                op="approve", auto=False, reason=f"{reason}，转人工复核", source="manual", gate=gate
            )
        return JoinDecision(
            op="decline",
            auto=True,
            confidence=1.0,
            reason=reason,
            risk="其他",
            source="rule",
            blacklist=blacklist,
            gate=gate,
        )

    async def _already_handled(self, request_id: str) -> bool:
        if request_id in self._seen:
            return True
        if self.audit is not None:
            existing = await self.audit.get_join(request_id)
            if existing and existing.get("decision") not in (None, "", "pending"):
                self._seen.add(request_id)
                return True
        return False

    async def judge(self, group_id: str, request: dict[str, Any], *, mode: str) -> JoinDecision:
        """规则 + LLM 判定一条入群申请。"""
        member_openid = str(request.get("member_openid") or "")
        risk_tips = str(request.get("risk_tips") or "")
        username = str(request.get("username") or "")
        verify = request.get("verify_info") or {}
        verify_message = str(verify.get("verify_message") or "")
        apply_source = str(request.get("apply_source") or "self_apply")
        profile = request.get("profile") or {}

        # 1) 硬规则
        if member_openid and member_openid in self.store.local_blacklist(group_id):
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="命中本地黑名单",
                risk="违规内容",
                source="rule",
            )
        # 1.5) 跨群黑名单（B5）：在 A 群被拉黑的人，B 群默认也拒绝入群
        global_check = getattr(self.store, "is_globally_blacklisted", None)
        if member_openid and callable(global_check) and global_check(member_openid):
            reason = ""
            reason_getter = getattr(self.store, "global_blacklist_reason", None)
            if callable(reason_getter):
                reason = str(reason_getter(member_openid) or "")
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="命中跨群黑名单" + (f"（{reason}）" if reason else ""),
                risk="违规内容",
                source="rule",
            )
        if str(request.get("bot", "")).lower() == "true" or request.get("bot") is True:
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="机器人账号不予放行",
                risk="其他",
                source="rule",
            )
        if risk_tips == "top_tips":
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=1.0,
                reason="平台风险提示（top_tips），自动拒绝",
                risk="违规内容",
                blacklist=True,
                source="rule",
            )
        settings = self.store.settings()
        profile_gate = self._profile_gate(request.get("profile") or {}, settings)
        if profile_gate is not None:
            return profile_gate
        answer_gate = self._answer_gate(verify, settings)
        if answer_gate is not None:
            return answer_gate
        if apply_source == "invited" and settings.get("join_trust_inviter", False):
            return JoinDecision(
                op="approve",
                auto=True,
                confidence=1.0,
                reason="邀请入群且已信任邀请人",
                source="rule",
            )

        # 2) 模式与 LLM
        if mode == "human" or self.judge_call is None:
            return JoinDecision(
                op="approve",
                auto=False,
                reason="等待人工审批" if mode == "human" else "未配置可用的对话模型，转人工",
                source="manual",
            )
        prompt_request = {
            "rules_brief": self._rules_brief(group_id, settings),
            "answer_expectation": self._answer_expectation(settings),
            "apply_source": {"self_apply": "主动申请", "invited": "被邀请"}.get(
                apply_source, apply_source
            ),
            "username": truncate(username, 40) or "未知",
            "qq_level": profile.get("qq_level") if profile.get("qq_level") is not None else "未知",
            "account_age_days": (
                profile.get("account_age_days")
                if profile.get("account_age_days") is not None
                else "未知"
            ),
            "reg_time": profile.get("reg_time") or "未知",
            "qid": str(profile.get("qid") or "无") or "无",
            "sex": str(profile.get("sex") or "未知") or "未知",
            "profile_state": self._profile_state(profile),
            "verify_method": str(verify.get("method") or "未知"),
            "verify_message": truncate(verify_message, 300) or "（无）",
            "review_qa": self._render_qa(verify.get("review_qa_list")),
            "risk_tips": risk_tips or "无",
            "is_bot": "否",
            "invited_by": str(request.get("invited_by") or "无") or "无",
        }
        user_prompt = JOIN_USER_TEMPLATE
        for key, value in prompt_request.items():
            user_prompt = user_prompt.replace("{" + key + "}", str(value))
        avatar = str(profile.get("avatar_url") or "")
        avatar_mode = str(settings.get("join_avatar_review") or "off")
        first_images = [avatar] if (avatar and avatar_mode == "always") else []
        try:
            raw = await self._call_judge(
                JOIN_SYSTEM_PROMPT, user_prompt, first_images, group_id
            )
        except Exception as exc:
            self.stats.failed += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            return JoinDecision(
                op="approve", auto=False, reason="模型调用失败，转人工", source="manual"
            )
        decision = self.parse_decision(raw, request, settings=settings, mode=mode)
        if avatar and avatar_mode == "approve_only":
            decision = await self._avatar_review(decision, avatar, profile, settings, group_id)
        return decision

    def _judge_accepts(self) -> set[str] | None:
        """judge_call 额外支持的关键字；None 表示「任意关键字都接受」。"""
        func = self.judge_call
        if self._judge_signature_for is not func:
            self._judge_signature_for = func
            names: set[str] | None = set()
            try:
                params = inspect.signature(func).parameters  # type: ignore[arg-type]
            except (TypeError, ValueError):
                names = None
            else:
                if any(item.kind is item.VAR_KEYWORD for item in params.values()):
                    names = None
                else:
                    names = {
                        name
                        for name, item in params.items()
                        if item.kind in (item.POSITIONAL_OR_KEYWORD, item.KEYWORD_ONLY)
                    }
            self._judge_kwargs = names
        return self._judge_kwargs

    async def _call_judge(
        self,
        system_prompt: str,
        user_prompt: str,
        image_urls: list[str] | None = None,
        group_id: str = "",
    ) -> str:
        """调用判定模型；按签名兼容只接受部分参数的旧替身/自定义实现。"""
        if self.judge_call is None:
            raise RuntimeError("未配置可用的对话模型")
        wanted: dict[str, Any] = {}
        if image_urls:
            wanted["image_urls"] = list(image_urls)
        if group_id:
            wanted["group_id"] = group_id
        accepts = self._judge_accepts()
        if accepts is not None:
            wanted = {key: value for key, value in wanted.items() if key in accepts}
        return await self.judge_call(system_prompt, user_prompt, **wanted)

    async def _avatar_review(
        self,
        decision: JoinDecision,
        avatar: str,
        profile: dict[str, Any],
        settings: dict[str, Any],
        group_id: str = "",
    ) -> JoinDecision:
        """头像多模态复核：只允许把「拟放行」收紧为拒绝，绝不反向放行。"""
        if not (decision.op == "approve" and decision.auto):
            return decision
        below = float(settings.get("join_avatar_only_below", 0.95) or 0.95)
        if decision.confidence >= below:
            return decision
        prompt = (
            AVATAR_USER_TEMPLATE.replace("{username}", str(profile.get("nickname") or "未知"))
            .replace(
                "{qq_level}",
                str(profile.get("qq_level") if profile.get("qq_level") is not None else "未知"),
            )
        )
        try:
            raw = await self._call_judge(AVATAR_SYSTEM_PROMPT, prompt, [avatar], group_id)
        except Exception as exc:
            if self.logger is not None:
                self.logger.debug("头像复核失败（保留原判定）：%s", exc)
            return decision
        payload = extract_json_object(raw or "")
        if not payload:
            return decision
        risk = payload.get("risk")
        risky = risk is True or str(risk or "").strip().lower() in ("true", "1", "yes")
        if not risky:
            return decision
        confidence = clamp_float(payload.get("confidence"), 0.0, 0.0, 1.0)
        threshold = float(settings.get("join_min_confidence", 0.8) or 0.8)
        if confidence < threshold:
            return decision
        return JoinDecision(
            op="decline",
            auto=True,
            confidence=confidence,
            reason=truncate(payload.get("reason") or "头像疑似违规", 60),
            risk="违规内容",
            blacklist=bool(settings.get("join_decline_blacklist", True)),
            source="llm",
            gate="avatar",
        )

    def parse_decision(
        self,
        raw: str,
        request: dict[str, Any],
        *,
        settings: dict[str, Any],
        mode: str,
    ) -> JoinDecision:
        """解析模型输出并按阈值决定是否自动执行。"""
        payload = extract_json_object(raw or "")
        if payload is None:
            return JoinDecision(
                op="approve",
                auto=False,
                confidence=0.0,
                reason="模型返回无法解析，转人工",
                source="manual",
            )
        decision = "approve" if str(payload.get("decision", "")).lower() == "approve" else "decline"
        confidence = clamp_float(payload.get("confidence"), 0.0, 0.0, 1.0)
        reason = truncate(payload.get("reason") or "", 60)
        risk = str(payload.get("risk") or "无")
        threshold = float(settings.get("join_min_confidence", 0.8) or 0.8)
        if str(request.get("risk_tips") or "") == "warning_tips":
            threshold = min(0.98, threshold + 0.1)
        if mode == "strict" and decision == "approve" and confidence < threshold:
            return JoinDecision(
                op="decline",
                auto=True,
                confidence=confidence,
                reason=reason or "严格模式下置信度不足，自动拒绝",
                risk=risk,
                source="llm",
            )
        auto = confidence >= threshold
        if not auto:
            return JoinDecision(
                op=decision,
                auto=False,
                confidence=confidence,
                reason=reason or "置信度不足，转人工",
                risk=risk,
                source="manual",
            )
        blacklist = decision == "decline" and bool(settings.get("join_decline_blacklist", True))
        return JoinDecision(
            op=decision,
            auto=True,
            confidence=confidence,
            reason=reason,
            risk=risk,
            blacklist=blacklist,
            source="llm",
        )

    def _rules_brief(self, group_id: str, settings: dict[str, Any]) -> str:
        config = self.store.group(group_id)
        if config is not None and config.rules_brief:
            return config.rules_brief
        return str(settings.get("group_rules_brief") or "（未配置，按通用社区规范判断）")

    @staticmethod
    def _render_qa(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return "（无）"
        lines = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            question = truncate(item.get("question") or "", 60)
            answer = truncate(item.get("answer") or "", 60)
            lines.append(f"问：{question} / 答：{answer}")
        return "\n".join(lines) or "（无）"

    # ------------------------------------------------------------------
    async def submit(
        self,
        group_id: str,
        request: dict[str, Any],
        decision: JoinDecision,
        *,
        by: str = "llm",
    ) -> dict[str, Any]:
        """执行审批（approve / decline）并落库。"""
        member_openid = str(request.get("member_openid") or "")
        request_id = str(request.get("join_request_id") or "")
        try:
            response = await self.api.approve_join_request(
                group_id,
                member_openid,
                op=decision.op,
                join_request_id=request_id,
                reject_reason=decision.reason if decision.op == "decline" else "",
                add_to_blacklist=bool(decision.blacklist),
                caller="join_review",
            )
        except Exception as exc:
            self.stats.failed += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            await self._persist(group_id, request, decision, decided_by=by, error=str(exc))
            return {"ok": False, "message": str(exc), "join_request_id": request_id}
        self._seen.add(request_id)
        self._pending.pop(request_id, None)
        if decision.op == "approve":
            self.stats.approved += 1
        else:
            self.stats.declined += 1
        await self._persist(group_id, request, decision, decided_by=by)
        return {
            "ok": True,
            "op": decision.op,
            "join_request_id": request_id,
            "dry_run": bool(isinstance(response, dict) and response.get("_dry_run")),
            "reason": decision.reason,
        }

    async def decide_manual(
        self,
        group_id: str,
        member_openid: str,
        *,
        op: str,
        join_request_id: str = "",
        reason: str = "",
        blacklist: bool = False,
        by: str = "human",
    ) -> dict[str, Any]:
        """人工审批（WebUI 或群指令）。"""
        request = {
            "member_openid": member_openid,
            "join_request_id": join_request_id,
        }
        if join_request_id and join_request_id in self._pending:
            request = self._pending[join_request_id]["request"]
        decision = JoinDecision(
            op="approve" if op == "approve" else "decline",
            auto=True,
            confidence=1.0,
            reason=reason or ("人工通过" if op == "approve" else "人工拒绝"),
            blacklist=blacklist,
            source="human",
        )
        result = await self.submit(group_id, request, decision, by=by)
        result["by"] = by
        del member_openid
        return result

    # ------------------------------------------------------------------
    async def _persist(
        self,
        group_id: str,
        request: dict[str, Any],
        decision: JoinDecision,
        *,
        decided_by: str,
        error: str = "",
    ) -> None:
        if self.audit is None:
            return
        verify = request.get("verify_info") or {}
        profile = request.get("profile") or {}
        await self.audit.record_join(
            join_request_id=str(request.get("join_request_id") or ""),
            group_id=group_id,
            member_openid=str(request.get("member_openid") or ""),
            union_openid=str(request.get("union_openid") or ""),
            username=str(request.get("username") or ""),
            apply_source=str(request.get("apply_source") or ""),
            invited_by=str(request.get("invited_by") or ""),
            is_bot=1 if request.get("bot") else 0,
            risk_tips=str(request.get("risk_tips") or ""),
            verify_method=str(verify.get("method") or ""),
            verify_message=truncate(verify.get("verify_message") or "", 300),
            review_qa=str(verify.get("review_qa_list") or ""),
            decision="pending" if decided_by == "pending" else decision.op,
            decided_by=decided_by,
            confidence=decision.confidence,
            reason=(decision.reason + (f" | 失败：{error}" if error else ""))[:200],
            blacklisted=1 if decision.blacklist else 0,
            profile_source=str(profile.get("source") or ""),
            avatar_url=str(profile.get("avatar_url") or ""),
            qq_level=profile.get("qq_level"),
            account_age_days=profile.get("account_age_days"),
            reg_time=profile.get("reg_time"),
            qid=str(profile.get("qid") or ""),
            profile_json=safe_json_dumps(profile) if profile else "",
            gate=decision.gate,
        )

    async def _notify_pending(
        self, group_id: str, request: dict[str, Any], decision: JoinDecision
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(
                "join_pending",
                {
                    "group_id": group_id,
                    "group_name": (
                        self.store.group(group_id).name if self.store.group(group_id) else ""
                    ),
                    "member_openid": str(request.get("member_openid") or ""),
                    "username": str(request.get("username") or ""),
                    "qq_level": (request.get("profile") or {}).get("qq_level"),
                    "account_age_days": (request.get("profile") or {}).get("account_age_days"),
                    "join_request_id": str(request.get("join_request_id") or ""),
                    "risk_tips": str(request.get("risk_tips") or ""),
                    "verify_message": truncate(
                        (request.get("verify_info") or {}).get("verify_message") or "", 80
                    ),
                    "suggestion": decision.reason,
                },
            )
        except Exception as exc:  # pragma: no cover
            if self.logger is not None:
                self.logger.warning("入群申请通知失败：%s", exc)

    # ------------------------------------------------------------------
    def list_pending(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """列出待人工审批的申请。"""
        items = list(self._pending.values())
        if group_id:
            items = [item for item in items if item.get("group_id") == group_id]
        return [dict(item) for item in items]

    def status(self) -> dict[str, Any]:
        """运行状态（供 WebUI）。"""
        return {
            "pending": len(self._pending),
            "seen": len(self._seen),
            **self.stats.to_dict(),
        }
