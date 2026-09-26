"""领域模型与常量（纯数据类，不依赖 AstrBot）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .utils import clamp_float, clamp_int, now_ts, optional_int

# --------------------------------------------------------------------------
# 能力标识
# --------------------------------------------------------------------------
CAP_GROUP_INFO = "group_info"
CAP_BOT_STATE = "bot_state"
CAP_IS_ADMIN = "is_admin"
CAP_FULL_MSG = "full_msg"
CAP_RECALL = "recall"
CAP_MUTE = "mute"
CAP_JOIN_REVIEW = "join_review"
CAP_MEMBER_LIST = "member_list"
CAP_BLACKLIST = "blacklist"
CAP_REMOVE_MEMBER = "remove_member"

CAPABILITIES: tuple[str, ...] = (
    CAP_GROUP_INFO,
    CAP_BOT_STATE,
    CAP_IS_ADMIN,
    CAP_FULL_MSG,
    CAP_RECALL,
    CAP_MUTE,
    CAP_JOIN_REVIEW,
    CAP_MEMBER_LIST,
    CAP_BLACKLIST,
    CAP_REMOVE_MEMBER,
)

CAPABILITY_LABELS: dict[str, str] = {
    CAP_GROUP_INFO: "群基本信息",
    CAP_BOT_STATE: "机器人群内状态",
    CAP_IS_ADMIN: "机器人是群管理员",
    CAP_FULL_MSG: "接收全部消息",
    CAP_RECALL: "撤回消息",
    CAP_MUTE: "成员禁言",
    CAP_JOIN_REVIEW: "入群申请审批",
    CAP_MEMBER_LIST: "群成员列表/详情",
    CAP_BLACKLIST: "群黑名单",
    CAP_REMOVE_MEMBER: "批量移除成员",
}

#: 内邀（灰度）能力：文档标注「该能力正在内邀接入中」
INVITE_ONLY_CAPABILITIES: tuple[str, ...] = (
    CAP_MEMBER_LIST,
    CAP_BLACKLIST,
    CAP_REMOVE_MEMBER,
)

# --------------------------------------------------------------------------
# QQ 错误码
# --------------------------------------------------------------------------
ERR_NOT_WHITELISTED = 11253
ERR_INTERFACE_FORBIDDEN = 11254
ERR_PRIVILEGE_CHECK_FAILED = 11252
ERR_ADMIN_CHECK_FAILED = 11281
ERR_NOT_ADMIN = 11282
ERR_ROBOT_BANNED = 11265
ERR_NOT_MEMBER = 1100301
ERR_GROUP_GONE = 1100104
ERR_RATE_LIMITED = 1100308
ERR_RECALL_EXPIRED = 40064004
ERR_RECALL_FORBIDDEN = 40062003
ERR_PROACTIVE_LIMIT = 40034100
ERR_MSG_ID_EXPIRED = 40034005
ERR_REQ_INVALID = 12002

#: 平台限制
MUTE_MAX_SECONDS = 30 * 86400
MUTE_BATCH_MAX = 20
JOIN_REQUEST_PAGE_MAX = 50
MEMBER_PAGE_SIZE = 30
BLACKLIST_PAGE_MAX = 100

MODERATION_MODES: tuple[str, ...] = ("strict", "standard", "lenient", "log_only")
JOIN_REVIEW_MODES: tuple[str, ...] = ("off", "strict", "standard", "human")
#: 画像不可得时的策略：pass 放行 / manual 转人工 / decline 拒绝
JOIN_PROFILE_MISSING_MODES: tuple[str, ...] = ("pass", "manual", "decline")
#: 画像硬规则命中后的动作
JOIN_GATE_ACTIONS: tuple[str, ...] = ("decline", "manual", "pass")
#: 头像多模态复核：off 关闭 / approve_only 仅复核拟放行的 / always 每次
JOIN_AVATAR_REVIEW_MODES: tuple[str, ...] = ("off", "approve_only", "always")

IMAGE_REVIEW_MODES: tuple[str, ...] = ("off", "with_text", "always")

RISK_CONDITION_PREFIX = "risk>="

SEND_CONDITIONS: tuple[str, ...] = (
    "rule_hit",
    "has_link",
    "has_contact",
    "ad_template",
    "has_image",
    "long_text",
    "new_member",
    "flood",
    "all",
)

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GroupProfile:
    """群基本信息（/v2/groups/{gid}/info）。"""

    group_openid: str
    name: str = ""
    memo: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    member_num: int = 0
    fetched_at: int = 0

    @classmethod
    def from_api(cls, group_openid: str, payload: dict[str, Any]) -> GroupProfile:
        tags = payload.get("group_tags") or []
        return cls(
            group_openid=group_openid,
            name=str(payload.get("group_name") or ""),
            memo=str(payload.get("group_finger_memo") or ""),
            category=str(payload.get("group_class_text") or ""),
            tags=[str(item) for item in tags if isinstance(item, (str, int))],
            member_num=clamp_int(payload.get("group_member_num"), 0, 0, 10**9),
            fetched_at=now_ts(),
        )


@dataclass(slots=True)
class BotState:
    """机器人群内状态（/v2/groups/{gid}/bot_state）。"""

    group_openid: str
    member_openid: str = ""
    joined_at: int | None = None
    allow_proactive_msg: bool = False
    recv_msg_setting: str = ""
    member_role: str = ""
    fetched_at: int = 0

    @property
    def is_admin(self) -> bool:
        return self.member_role in {"admin", "owner"}

    @property
    def full_msg(self) -> bool:
        """是否已开启「接收全部消息」（审核启用的硬性前提）。"""
        return self.recv_msg_setting == "all"


@dataclass(slots=True)
class CapabilityResult:
    """单项能力的探测结果。"""

    capability: str
    ok: bool
    err_code: int | None = None
    trace_id: str = ""
    note: str = ""
    checked_at: int = 0
    probed: bool = True
    """False 表示该能力没有只读探测接口（例如批量移除成员），只能按需尝试。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, capability: str, payload: dict[str, Any]) -> CapabilityResult:
        return cls(
            capability=capability,
            ok=bool(payload.get("ok")),
            err_code=payload.get("err_code"),
            trace_id=str(payload.get("trace_id") or ""),
            note=str(payload.get("note") or ""),
            checked_at=clamp_int(payload.get("checked_at"), 0, 0, 2**31),
            probed=bool(payload.get("probed", True)),
        )


@dataclass(slots=True)
class GroupConfig:
    """单群插件配置（存 KV groups[group_id]）。"""

    group_id: str
    platform_id: str = ""
    name: str = ""
    moderation_enabled: bool = False
    mode: str = ""
    join_review_mode: str = ""
    rules_brief: str = ""
    notify_session: str = ""
    paused_reason: str = ""
    trusted: list[str] = field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_seen: int = 0
    added_at: int = 0
    source: str = "auto"  # auto=消息自动登记，manual=WebUI 添加

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GroupConfig:
        group_id = str(payload.get("group_id") or "")
        trusted = payload.get("trusted") or []
        capabilities = payload.get("capabilities") or {}
        return cls(
            group_id=group_id,
            platform_id=str(payload.get("platform_id") or ""),
            name=str(payload.get("name") or ""),
            moderation_enabled=bool(payload.get("moderation_enabled")),
            mode=str(payload.get("mode") or ""),
            join_review_mode=str(payload.get("join_review_mode") or ""),
            rules_brief=str(payload.get("rules_brief") or ""),
            notify_session=str(payload.get("notify_session") or ""),
            paused_reason=str(payload.get("paused_reason") or ""),
            trusted=[str(item) for item in trusted if str(item).strip()],
            capabilities={
                str(key): dict(value)
                for key, value in capabilities.items()
                if isinstance(value, dict)
            },
            last_seen=clamp_int(payload.get("last_seen"), 0, 0, 2**31),
            added_at=clamp_int(payload.get("added_at"), 0, 0, 2**31),
            source=str(payload.get("source") or "auto"),
        )

    def capability_ok(self, capability: str) -> bool:
        """能力是否可用；未探测过时视为未知（False）。"""
        record = self.capabilities.get(capability)
        return bool(record and record.get("ok"))


@dataclass(slots=True)
class ApplicantProfile:
    """入群申请人画像。

    OneBot（NapCat）可拿到账号等级与注册时间，官方通道只有昵称 —— 拿不到的字段
    一律留 None 并置 degraded=True，由上层按「资料缺失策略」处理，绝不臆测。
    """

    platform_id: str = ""
    kind: str = ""
    user_id: str = ""
    nickname: str = ""
    avatar_url: str = ""
    qq_level: int | None = None
    qid: str = ""
    sex: str = ""
    age: int | None = None
    reg_time: int | None = None
    account_age_days: int | None = None
    is_vip: bool = False
    vip_level: int = 0
    source: str = "none"  # onebot_stranger / official_request / cache / none
    degraded: bool = True
    #: True 表示「远端调用失败」；degraded 也可能只是协议端没有该字段
    failed: bool = False
    note: str = ""
    fetched_at: int = 0

    @property
    def has_account_signals(self) -> bool:
        """是否拿到账号维度信号（等级或注册时间）。"""
        return self.qq_level is not None or self.account_age_days is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> ApplicantProfile:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            platform_id=str(payload.get("platform_id") or ""),
            kind=str(payload.get("kind") or ""),
            user_id=str(payload.get("user_id") or ""),
            nickname=str(payload.get("nickname") or ""),
            avatar_url=str(payload.get("avatar_url") or ""),
            qq_level=optional_int(payload.get("qq_level")),
            qid=str(payload.get("qid") or ""),
            sex=str(payload.get("sex") or ""),
            age=optional_int(payload.get("age")),
            reg_time=optional_int(payload.get("reg_time")),
            account_age_days=optional_int(payload.get("account_age_days")),
            is_vip=bool(payload.get("is_vip")),
            vip_level=optional_int(payload.get("vip_level")) or 0,
            source=str(payload.get("source") or "none"),
            degraded=bool(payload.get("degraded", True)),
            failed=bool(payload.get("failed")),
            note=str(payload.get("note") or ""),
            fetched_at=clamp_int(payload.get("fetched_at"), 0, 0, 2**31),
        )


@dataclass(slots=True)
class ActionResult:
    """一次处置动作的执行结果。"""

    action: str
    ok: bool
    dry_run: bool = False
    err_code: int | None = None
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Verdict:
    """内容审核判定结果（M2 使用，M1 先定义数据结构）。"""

    verdict: str = "review"
    category: str = "无"
    severity: int = 1
    confidence: float = 0.0
    reason: str = ""
    suggested_action: str = "none"
    source: str = "llm"  # llm / rule / manual
    raw: str = ""
    parse_error: bool = False
    latency_ms: int = 0
    #: 图片中二维码承载的文本（由多模态模型填写；解析不到即忽略）
    qr_text: str = ""
    #: 模型给出的判断过程（先推理后结论，供审计与调参）
    analysis: str = ""
    #: 模型逐字摘录的判断依据（找不到可执行渠道时为空）
    evidence: str = ""

    @property
    def is_violation(self) -> bool:
        return self.verdict == "violation"

    @classmethod
    def review(cls, reason: str, *, source: str = "llm") -> Verdict:
        """构造"需要人工复核"的安全默认值。"""
        return cls(verdict="review", reason=reason, source=source, confidence=0.0)

    def clamped(self) -> Verdict:
        """把 LLM 返回的不合法字段钳制回合法范围。"""
        verdict = self.verdict if self.verdict in {"allow", "review", "violation"} else "review"
        severity = clamp_int(self.severity, 1, 1, 5)
        confidence = clamp_float(self.confidence, 0.0, 0.0, 1.0)
        return Verdict(
            verdict=verdict,
            category=self.category or "无",
            severity=severity,
            confidence=confidence,
            reason=self.reason[:200],
            suggested_action=self.suggested_action or "none",
            source=self.source,
            raw=self.raw,
            parse_error=self.parse_error,
            latency_ms=self.latency_ms,
            qr_text=self.qr_text[:300],
            analysis=self.analysis[:500],
            evidence=self.evidence[:300],
        )


def default_settings() -> dict[str, Any]:
    """首次安装时的默认配置（dry_run + lenient：只记录、只警告）。"""
    return {
        "enabled": True,
        "dry_run": True,
        "dry_run_warn": True,
        "default_moderation_enabled": False,
        "allow_without_full_msg": False,
        "mode": "lenient",
        "sample_rate": 1.0,
        "send_conditions": ["rule_hit", "risk>=60"],
        "risk_send_threshold": 60,
        "normalize_enabled": True,
        "homoglyph_enabled": True,
        "template_enabled": True,
        "pinyin_enabled": False,
        "fuzzy_max_distance": 1,
        "auto_enforce_normalized": False,
        "duplicate_flood_window": 300,
        "duplicate_flood_members": 3,
        # 竞赛域名白名单：白名单内链接不计 link 分（只降权，不豁免规则与送审）
        "domain_allowlist_enabled": True,
        "domain_allowlist": [
            "ac.nowcoder.com",
            "nowcoder.com",
            "codeforces.com",
            "atcoder.jp",
            "luogu.com.cn",
            "luogu.com",
            "xcpc.link",
            "xcpc.ink",
            "vjudge.net",
            "codechef.com",
            "icpc.global",
            "hdu.edu.cn",
        ],
        "llm_min_confidence": 0.7,
        "llm_timeout": 20,
        "llm_qpm_per_group": 20,
        "llm_max_concurrency": 4,
        "llm_daily_budget": 0,
        "llm_provider_id": "",
        # 图片审核：off 不送审（默认）／with_text 仅图文混排时把图一起送／always 纯图片也送
        "image_review": "off",
        "image_review_max": 1,
        "cache_ttl": 600,
        "circuit_break_threshold": 5,
        # 送审时附带最近 N 条群消息作为语境（0 = 关闭，退回"只看单条消息"）
        "llm_context_messages": 8,
        "block_llm_on_violation": False,
        # 标准档处置矩阵；首次安装以 mode=lenient 兜底（只保留 warn/report），
        # 因此"安装即温和"与"切到标准档即完整处置"两个诉求可以同时成立。
        "action_matrix": {
            "violation": {
                "1": ["warn"],
                "2": ["warn"],
                "3": ["warn", "mute"],
                "4": ["recall", "mute"],
                "5": ["recall", "mute", "report"],
            }
        },
        "mute_steps": {"3": 600, "4": 3600, "5": 86400},
        "max_mute_days": 30,
        "repeat_offense_multiplier": True,
        "auto_blacklist": False,
        "auto_remove": False,
        "join_review_mode": "off",
        "join_poll_interval": 60,
        "join_min_confidence": 0.8,
        "join_decline_blacklist": True,
        "join_trust_inviter": False,
        # 入群申请人画像（默认全关：升级后行为与旧版一致）
        "join_profile_enabled": True,
        "join_min_account_days": 0,
        "join_min_qq_level": 0,
        "join_require_qid": False,
        "join_gate_action": "decline",
        "join_profile_missing": "manual",
        "join_avatar_review": "off",
        "join_avatar_only_below": 0.95,
        "join_profile_cache_days": 7,
        "join_profile_qpm": 30,
        "join_profile_concurrency": 2,
        "notify_session": "",
        # 申诉闭环：默认接受申诉；误判自学习白名单默认关（避免被社工利用）
        "appeal_enabled": True,
        "appeal_auto_whitelist": False,
        "appeal_notify": True,
        "db_path": "",
        "retention_events_days": 30,
        "retention_api_days": 7,
        "retention_capability_days": 30,
        "retention_join_days": 90,
        "store_text": False,
        "audit_queue_maxsize": 5000,
        "group_rules_brief": "",
        "prompt_system": "",
        "prompt_user": "",
        "flood_threshold": 8,
        "probe_full_msg_interval": 1800,
        "capability_log_dedupe_window": 1800,
    }


#: 需要在 WebUI 中被钳制的数值型配置：(最小, 最大)
NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "sample_rate": (0.0, 1.0),
    "llm_min_confidence": (0.0, 1.0),
    "llm_timeout": (5, 120),
    "llm_qpm_per_group": (1, 120),
    "llm_max_concurrency": (1, 16),
    "llm_daily_budget": (0, 1_000_000),
    "cache_ttl": (0, 86400),
    "circuit_break_threshold": (1, 50),
    "llm_context_messages": (0, 20),
    "max_mute_days": (1, 30),
    "join_poll_interval": (30, 600),
    "join_min_confidence": (0.0, 1.0),
    "join_min_account_days": (0, 3650),
    "join_min_qq_level": (0, 144),
    "join_avatar_only_below": (0.0, 1.0),
    "join_profile_cache_days": (0, 90),
    "join_profile_qpm": (1, 300),
    "join_profile_concurrency": (1, 8),
    "retention_events_days": (1, 365),
    "retention_api_days": (1, 365),
    "retention_capability_days": (1, 365),
    "retention_join_days": (1, 365),
    "audit_queue_maxsize": (100, 100_000),
    "flood_threshold": (0, 100),
    "image_review_max": (1, 4),
    "risk_send_threshold": (0, 100),
    "fuzzy_max_distance": (0, 3),
    "duplicate_flood_window": (30, 3600),
    "duplicate_flood_members": (2, 20),
    "probe_full_msg_interval": (300, 86400),
    "capability_log_dedupe_window": (0, 86400),
}
