"""指令定义、匹配与回复文案。

指令采用「全匹配 + 空格分词」，不依赖 AstrBot 的唤醒判定，因此在群开启
「接收全部消息」后依然可用（普通聊天不会误触发，因为必须完整等于指令名）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .api_client import QQApiError
from .models import (
    CAP_FULL_MSG,
    CAP_IS_ADMIN,
    CAPABILITY_LABELS,
    BotState,
    GroupProfile,
)
from .utils import human_duration, mask_openid, to_iso

# --------------------------------------------------------------------------
# 指令名集合
# --------------------------------------------------------------------------
MENU_COMMANDS = ("群管理菜单", "群管菜单", "qq群管理")
INFO_COMMANDS = ("群信息",)
STATUS_COMMANDS = ("审核状态",)
SELFCHECK_COMMANDS = ("群管理自检",)
CONFIG_COMMANDS = ("群管理配置",)
TOGGLE_COMMANDS = ("审核开启", "审核关闭")
MODE_COMMANDS = ("审核模式",)
THRESHOLD_COMMANDS = ("审核阈值",)
KEYWORD_COMMANDS = ("关键词",)
TRUST_COMMANDS = ("信任", "取消信任")
MUTE_COMMANDS = ("禁言",)
UNMUTE_COMMANDS = ("解禁",)
RECALL_COMMANDS = ("撤回",)
LOG_COMMANDS = ("审核日志",)
STATS_COMMANDS = ("审核统计",)
APPEAL_COMMANDS = ("申诉",)
GLOBAL_BLACKLIST_COMMANDS = ("全局拉黑", "全局解除")
APPEAL_DECIDE_COMMANDS = ("申诉通过", "申诉驳回")
JOIN_MODE_COMMANDS = ("入群审核",)
JOIN_LIST_COMMANDS = ("入群申请",)
JOIN_APPROVE_COMMANDS = ("入群通过",)
JOIN_DECLINE_COMMANDS = ("入群拒绝",)
BLACKLIST_COMMANDS = ("黑名单",)
DRYRUN_COMMANDS = ("dry-run", "dryrun", "演练模式", "DryRun")

#: 需要群管理员权限的指令（AstrBot 管理员同样可用）
GROUP_ADMIN_COMMANDS: tuple[str, ...] = (
    *TOGGLE_COMMANDS,
    *MODE_COMMANDS,
    *THRESHOLD_COMMANDS,
    *KEYWORD_COMMANDS,
    *TRUST_COMMANDS,
    *MUTE_COMMANDS,
    *UNMUTE_COMMANDS,
    *RECALL_COMMANDS,
    *LOG_COMMANDS,
    *STATS_COMMANDS,
    *APPEAL_DECIDE_COMMANDS,
    *JOIN_MODE_COMMANDS,
    *JOIN_LIST_COMMANDS,
    *JOIN_APPROVE_COMMANDS,
    *JOIN_DECLINE_COMMANDS,
    *BLACKLIST_COMMANDS,
    *DRYRUN_COMMANDS,
)

#: 仅 AstrBot 管理员可用
ADMIN_ONLY_COMMANDS: tuple[str, ...] = (
    *SELFCHECK_COMMANDS,
    *CONFIG_COMMANDS,
    *GLOBAL_BLACKLIST_COMMANDS,
)

#: 所有人可用
PUBLIC_COMMANDS: tuple[str, ...] = (
    *MENU_COMMANDS,
    *INFO_COMMANDS,
    *STATUS_COMMANDS,
    *APPEAL_COMMANDS,
)

ALL_COMMANDS: tuple[str, ...] = tuple(
    dict.fromkeys((*PUBLIC_COMMANDS, *GROUP_ADMIN_COMMANDS, *ADMIN_ONLY_COMMANDS))
)

FULL_MSG_GUIDE = (
    "请用手机 QQ 打开本群 → 右上角「设置」→「机器人」→ 选中本机器人 → "
    "打开「接收全部消息」，然后回到管理台点「重新检测」。\n"
    "（未开启时平台只会把 @机器人 的消息推送给机器人，审核会漏掉大量内容，"
    "因此插件拒绝在未开启时启用审核。）"
)

WEBUI_HINT = "完整配置与日志：AstrBot WebUI → 插件管理 → 「QQ群管理」→ 管理台页面。"

MODE_FOLLOW = ("跟随", "跟随全局", "默认", "继承", "global", "follow")

MODE_LABELS = {
    "strict": "严格",
    "standard": "标准",
    "lenient": "宽松",
    "log_only": "仅记录",
}

JOIN_MODE_LABELS = {
    "off": "关闭",
    "strict": "严格（仅高置信通过）",
    "standard": "标准（高置信自动，其余转人工）",
    "human": "全部人工",
}


def mode_label_with_source(mode: str, group_mode: str, global_mode: str) -> str:
    """展示生效模式及其来源（群级覆盖 / 跟随全局）。"""
    if group_mode:
        return f"{MODE_LABELS.get(group_mode, group_mode)}（群级覆盖）"
    return f"{MODE_LABELS.get(global_mode, global_mode)}（跟随全局）"


def match_command(text: str) -> tuple[str, list[str]]:
    """全匹配指令（支持带参数），返回 (指令名, 参数列表)。"""
    if not text:
        return "", []
    parts = text.split(" ")
    name = parts[0]
    if name in ALL_COMMANDS:
        return name, parts[1:]
    return "", []


# --------------------------------------------------------------------------
# 文案
# --------------------------------------------------------------------------
def menu_text() -> str:
    """菜单文本（供指令与 menu.md 复用）。"""
    return (
        "QQ群管理\n"
        "──────────────\n"
        "所有人\n"
        "• 群信息 ─ 本群档案与机器人在群状态\n"
        "• 审核状态 ─ 审核开关、模式与我的豁免状态\n"
        "• 申诉 <理由> ─ 对被处置的消息提出申诉（回复原消息）\n"
        "• 群管理菜单 ─ 显示本菜单\n"
        "──────────────\n"
        "群主 / 群管理员\n"
        "• 审核开启 / 审核关闭\n"
        "• 审核模式 严格/标准/宽松/仅记录\n"
        "• 审核阈值 0.0-1.0\n"
        "• 关键词 添加/删除/列表 · 信任 @某人 · 取消信任 @某人\n"
        "• 禁言 @某人 [时长] · 解禁 @某人 · 撤回（引用消息）\n"
        "• 审核日志 [条数] · 审核统计 [今日/7天]\n"
        "• 申诉通过 / 申诉驳回 [理由] ─ 回复申诉消息处理\n"
        "• 入群审核 开启/关闭/模式 <模式> · 入群申请\n"
        "• 入群通过 <序号> · 入群拒绝 <序号> [理由]\n"
        "• 黑名单 添加/移除/列表\n"
        "• dry-run ─ 查看当前运行模式；dry-run 关闭/开启 ─ 切换实际处置\n"
        "──────────────\n"
        "AstrBot 管理员\n"
        "• 群管理自检 ─ 平台能力探测\n"
        "• 群管理配置 ─ 打开管理台指引\n"
        "──────────────\n"
        "提示：指令为全匹配，可带 / 前缀。"
    )


def group_info_text(
    profile: GroupProfile | None,
    state: BotState | None,
    *,
    group_id: str,
    profile_error: QQApiError | None = None,
    state_error: QQApiError | None = None,
) -> str:
    """群档案 + 机器人在群状态。"""
    lines = [f"群档案（{mask_openid(group_id)}）"]
    if profile is not None:
        lines.append(f"• 群名称：{profile.name or '（未返回）'}")
        if profile.memo:
            lines.append(f"• 群简介：{profile.memo}")
        if profile.category:
            lines.append(f"• 分类：{profile.category}")
        if profile.tags:
            lines.append(f"• 标签：{'、'.join(profile.tags[:8])}")
        if profile.member_num:
            lines.append(f"• 成员数：{profile.member_num}")
    else:
        lines.append("• 群基本信息：平台未返回")
        if profile_error is not None:
            lines.append(f"  └ {profile_error.hint or profile_error.message}")
    lines.append("")
    lines.append("机器人在群状态")
    if state is not None:
        role_label = {"member": "普通成员", "admin": "管理员", "owner": "群主"}.get(
            state.member_role, state.member_role or "未知"
        )
        recv_label = {
            "all": "接收全部消息",
            "only_mention": "仅 @机器人 的消息",
            "mention_and_context": "@机器人及相关上下文",
        }.get(state.recv_msg_setting, state.recv_msg_setting or "未知")
        lines.append(f"• 群内角色：{role_label}")
        lines.append(f"• 消息接收：{recv_label}")
        lines.append(f"• 可主动推送：{'是' if state.allow_proactive_msg else '否'}")
        if state.joined_at:
            lines.append(f"• 入群时间：{to_iso(state.joined_at)}")
    else:
        lines.append("• 群内状态：平台未返回")
        if state_error is not None:
            lines.append(f"  └ {state_error.hint or state_error.message}")
    return "\n".join(lines)


def moderation_status_text(
    *,
    group_id: str,
    enabled: bool,
    mode: str,
    paused_reason: str,
    full_msg: bool | None,
    is_admin: bool | None,
    is_exempt: bool,
    dry_run: bool,
    join_mode: str = "off",
    stats: dict[str, Any] | None = None,
) -> str:
    """本群审核状态。"""
    lines = [f"审核状态（{mask_openid(group_id)}）"]
    if enabled and not paused_reason:
        lines.append("• 审核：已开启")
    elif paused_reason:
        lines.append(f"• 审核：已暂停（{paused_reason}）")
    else:
        lines.append("• 审核：未开启")
    lines.append(f"• 模式：{mode}")
    lines.append(f"• 入群审批：{JOIN_MODE_LABELS.get(join_mode, join_mode or '关闭')}")
    if dry_run:
        lines.append("• 运行模式：dry-run（只记录、不实际处置）")
    if full_msg is False:
        lines.append("• 全量消息：未开启（无法启用审核）")
    elif full_msg is True:
        lines.append("• 全量消息：已开启")
    if is_admin is False:
        lines.append("• 机器人权限：非群管理员（无法撤回/禁言）")
    elif is_admin is True:
        lines.append("• 机器人权限：群管理员")
    lines.append(f"• 我是否豁免审核：{'是' if is_exempt else '否'}")
    if stats:
        lines.append(
            "• 今日：审核 {total} 条，违规 {violation} 条，待复核 {review} 条".format(
                total=stats.get("events_total", 0),
                violation=(stats.get("verdicts") or {}).get("violation", 0),
                review=(stats.get("verdicts") or {}).get("review", 0),
            )
        )
    return "\n".join(lines)


def _capability_line(name: str, result: Any) -> str:
    label = CAPABILITY_LABELS.get(name, name)
    if not getattr(result, "probed", True):
        return f"• {label}：无只读探测接口（按需尝试）"
    if result.ok:
        return f"• {label}：可用" + (f"（{result.note}）" if result.note else "")
    detail = result.note or ""
    code = f"err_code={result.err_code}" if result.err_code else ""
    suffix = "，".join(part for part in (code, detail) if part)
    return f"• {label}：不可用" + (f"（{suffix}）" if suffix else "")


def selfcheck_text(group_id: str, results: dict[str, Any], *, group_name: str = "") -> str:
    """能力自检报告。"""
    lines = [f"能力自检（{group_name or mask_openid(group_id)}）", "──────────────"]
    for name in (
        "group_info",
        "bot_state",
        "is_admin",
        "full_msg",
        "recall",
        "mute",
        "join_review",
        "member_list",
        "blacklist",
        "remove_member",
    ):
        result = results.get(name)
        if result is not None:
            lines.append(_capability_line(name, result))
    lines.append("──────────────")
    full_msg = results.get(CAP_FULL_MSG)
    is_admin = results.get(CAP_IS_ADMIN)
    if full_msg is not None and not full_msg.ok:
        lines.append(FULL_MSG_GUIDE.splitlines()[0])
    if is_admin is not None and not is_admin.ok:
        lines.append("需要撤回/禁言/入群审批时，请把机器人设为群管理员。")
    return "\n".join(lines)


def suggestions_for(group_id: str, results: dict[str, Any]) -> list[str]:
    """根据探测结果生成处置建议（WebUI 使用）。"""
    tips: list[str] = []
    for name, result in results.items():
        if getattr(result, "ok", False) or not getattr(result, "probed", True):
            continue
        hint = result.note or ""
        if result.err_code == 11253:
            hint = "该接口仅白名单机器人可用，请向 QQ 开放平台申请权限"
        tips.append(f"{CAPABILITY_LABELS.get(name, name)}：{hint or '不可用'}")
    return tips


def keyword_text(rules: dict[str, list[dict[str, Any]]], group_id: str) -> str:
    """关键词列表文案。"""
    lines = ["本地审核规则", "──────────────"]
    for bucket, label in (("hard", "硬规则（命中即处置）"), ("soft", "软规则（提升关注）")):
        lines.append(f"【{label}】")
        items = [
            item
            for item in (rules.get(bucket) or [])
            if isinstance(item, dict) and str(item.get("scope") or "all") in ("all", group_id)
        ]
        if not items:
            lines.append("（空）")
            continue
        for index, item in enumerate(items[:20], start=1):
            scope = "全局" if str(item.get("scope") or "all") == "all" else "本群"
            actions = ",".join(item.get("action") or []) or "按矩阵"
            state = "启用" if item.get("enabled", True) else "停用"
            lines.append(f"{index}. [{scope}|{state}] {item.get('pattern')} → {actions}")
    lines.append("──────────────")
    lines.append("用法：关键词 添加 硬 加群 / 关键词 删除 加群")
    return "\n".join(lines)


def log_text(rows: list[dict[str, Any]]) -> str:
    """审核日志文案。"""
    if not rows:
        return "暂无审核记录。"
    lines = ["最近审核记录", "──────────────"]
    for row in rows[:10]:
        lines.append(
            "{ts} [{verdict}/{category}] sev={sev} conf={conf:.2f} {name}: {reason}".format(
                ts=str(row.get("ts") or "")[5:19].replace("T", " "),
                verdict=row.get("verdict") or "-",
                category=row.get("category") or "-",
                sev=row.get("severity") or "-",
                conf=float(row.get("confidence") or 0.0),
                name=str(row.get("sender_name") or "?")[:12],
                reason=str(row.get("reason") or "")[:30],
            )
        )
    return "\n".join(lines)


def stats_text(summary: dict[str, Any], *, days: int) -> str:
    """审核统计文案。"""
    verdicts = summary.get("verdicts") or {}
    lines = [f"审核统计（近 {days} 天）", "──────────────"]
    lines.append(f"• 审核总量：{summary.get('events_total', 0)}")
    lines.append(
        "• 正常：{allow}　可疑：{review}　违规：{violation}".format(
            allow=verdicts.get("allow", 0),
            review=verdicts.get("review", 0),
            violation=verdicts.get("violation", 0),
        )
    )
    actions = summary.get("actions") or {}
    if actions:
        lines.append("• 处置动作：")
        for action, bucket in sorted(actions.items()):
            lines.append(f"  - {action}：成功 {bucket.get('ok', 0)} / 失败 {bucket.get('fail', 0)}")
    api_errors = summary.get("api_errors") or []
    if api_errors:
        lines.append("• 接口错误 Top：")
        for item in api_errors[:5]:
            lines.append(f"  - err_code={item.get('err_code')} × {item.get('count')}")
    capability_denied = summary.get("capability_denied") or []
    if capability_denied:
        lines.append("• 能力受限：")
        for item in capability_denied[:5]:
            label = CAPABILITY_LABELS.get(str(item.get("capability")), item.get("capability"))
            lines.append(f"  - {label}：err_code={item.get('err_code')} × {item.get('count')}")
    return "\n".join(lines)


def mutes_text(rows: list[dict[str, Any]]) -> str:
    """禁言台账文案。"""
    if not rows:
        return "当前没有生效中的禁言记录。"
    lines = ["禁言台账", "──────────────"]
    for row in rows[:20]:
        lines.append(
            "• {name}（{oid}）至 {until}".format(
                name=str(row.get("username") or "未知")[:16],
                oid=mask_openid(row.get("member_openid")),
                until=str(row.get("until_ts") or "")[5:16].replace("T", " "),
            )
        )
    lines.append("──────────────")
    lines.append("用法：解禁 @某人")
    return "\n".join(lines)


def blacklist_text(platform: list[dict[str, Any]], local: list[str], *, error: str = "") -> str:
    """黑名单文案（平台 + 本地）。"""
    lines = ["群黑名单", "──────────────"]
    lines.append(f"【平台黑名单】{len(platform)} 人")
    for item in platform[:10]:
        lines.append(
            "• {name}（{oid}）{at}".format(
                name=str(item.get("username") or "未知")[:16],
                oid=mask_openid(item.get("member_openid")),
                at=str(item.get("banned_at") or "")[5:16].replace("T", " "),
            )
        )
    if error:
        lines.append(f"  └ 平台接口不可用：{error}")
    lines.append(f"【本地黑名单】{len(local)} 人（仅影响插件判定）")
    for openid in local[:10]:
        lines.append(f"• {mask_openid(openid)}")
    lines.append("──────────────")
    lines.append("用法：黑名单 添加 @某人 / 黑名单 移除 @某人")
    return "\n".join(lines)


def join_list_text(pending: list[dict[str, Any]]) -> str:
    """待审入群申请文案。"""
    if not pending:
        return "当前没有待人工审批的入群申请。"
    lines = ["待审入群申请", "──────────────"]
    for index, item in enumerate(pending[:10], start=1):
        request = item.get("request") or {}
        verify = request.get("verify_info") or {}
        profile = request.get("profile") or {}
        marks: list[str] = []
        if isinstance(profile.get("qq_level"), int):
            marks.append(f"等级 {profile['qq_level']}")
        if isinstance(profile.get("account_age_days"), int):
            marks.append(f"账号 {profile['account_age_days']} 天")
        extra = "｜" + "｜".join(marks) if marks else ""
        lines.append(
            "{idx}. {name}{extra}｜来源 {source}｜验证：{verify}".format(
                idx=index,
                name=str(request.get("username") or "未知")[:16],
                extra=extra,
                source=str(request.get("apply_source") or "-"),
                verify=str(verify.get("verify_message") or "（无）")[:40],
            )
        )
        suggestion = (item.get("decision") or {}).get("reason")
        if suggestion:
            lines.append(f"   建议：{suggestion}")
    lines.append("──────────────")
    lines.append("用法：入群通过 <序号> / 入群拒绝 <序号> <理由>")
    return "\n".join(lines)


def mute_usage_text() -> str:
    return "用法：禁言 @某人 [时长]，例如「禁言 @张三 10分钟」（默认 10 分钟，最长 30 天）。"


def format_duration(seconds: int | None) -> str:
    return human_duration(seconds)


def join_lines(items: Iterable[str]) -> str:
    return "\n".join(str(item) for item in items)
