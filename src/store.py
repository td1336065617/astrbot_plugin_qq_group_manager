"""配置与运行状态存储（KV）+ 并发保护。

- 配置（settings / keywords / trusted / UI 状态）存 AstrBot 插件 KV，体量小、随热重载生效。
- 群列表与能力矩阵也在 KV（groups 键），内存里保留一份 GroupConfig 便于频繁读取。
- 审计类数据不走这里，见 audit.py。
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Protocol, runtime_checkable

from .models import (
    IMAGE_REVIEW_MODES,
    JOIN_ANSWER_ACTIONS,
    JOIN_AVATAR_REVIEW_MODES,
    JOIN_GATE_ACTIONS,
    JOIN_PROFILE_MISSING_MODES,
    JOIN_REVIEW_MODES,
    MODERATION_MODES,
    NUMERIC_BOUNDS,
    RISK_CONDITION_PREFIX,
    SEND_CONDITIONS,
    CapabilityResult,
    GroupConfig,
    default_settings,
)
from .utils import clamp_float, clamp_int, now_ts

KEY_SETTINGS = "settings"
KEY_GROUPS = "groups"
KEY_KEYWORDS = "keywords"
KEY_TRUSTED = "trusted"
KEY_ROLE_CACHE = "role_cache"
KEY_MEMBER_CACHE = "member_cache"
KEY_LOCAL_BLACKLIST = "local_blacklist"
#: 跨群黑名单：{openid: {"reason","added_by","added_at"}}，对全插件生效
KEY_GLOBAL_BLACKLIST = "global_blacklist"
KEY_UI_STATE = "ui_state"
KEY_JOIN_CURSOR = "join_cursor"
#: 申请人画像缓存：{“{platform_id}:{user_id}”: ApplicantProfile}
KEY_PROFILE_CACHE = "profile_cache"
KEY_TEMPLATES = "templates"
KEY_HOMOGLYPH = "homoglyph"

MEMBER_CACHE_PER_GROUP = 2000
ROLE_CACHE_TTL = 7 * 86400


@runtime_checkable
class KVBackend(Protocol):
    """AstrBot 插件 KV 的最小接口。"""

    async def get(self, key: str, default: Any = None) -> Any: ...

    async def put(self, key: str, value: Any) -> None: ...


class AstrBotKVBackend:
    """基于 Star 的 PluginKVStoreMixin（put_kv_data / get_kv_data）。"""

    def __init__(self, star: Any) -> None:
        self._star = star

    async def get(self, key: str, default: Any = None) -> Any:
        value = await self._star.get_kv_data(key, default)
        return default if value is None else value

    async def put(self, key: str, value: Any) -> None:
        await self._star.put_kv_data(key, value)


def normalize_settings(raw: Any) -> dict[str, Any]:
    """把任意来源的配置归一化：补默认值、钳制数值、校验枚举。"""
    settings = default_settings()
    if isinstance(raw, dict):
        settings.update({k: v for k, v in raw.items() if k in settings})
    defaults = default_settings()
    for key, (minimum, maximum) in NUMERIC_BOUNDS.items():
        # 兜底值一律取内置默认值：用户可以传入任意脏数据（WebUI 手填、旧配置、
        # 非法字符串），原先用 int(当前值) 兜底会直接抛 ValueError 导致保存失败。
        fallback = defaults.get(key, settings.get(key))
        current = settings.get(key)
        if isinstance(fallback, int) and not isinstance(fallback, bool):
            settings[key] = clamp_int(current, int(fallback), int(minimum), int(maximum))
        else:
            settings[key] = clamp_float(current, float(fallback), minimum, maximum)
    for key in (
        "enabled",
        "dry_run",
        "dry_run_warn",
        "default_moderation_enabled",
        "allow_without_full_msg",
        "block_llm_on_violation",
        "repeat_offense_multiplier",
        "auto_blacklist",
        "auto_remove",
        "join_decline_blacklist",
        "join_trust_inviter",
        "join_profile_enabled",
        "join_require_qid",
        "join_answer_case_sensitive",
        "store_text",
        "domain_allowlist_enabled",
        "appeal_enabled",
        "appeal_auto_whitelist",
        "appeal_notify",
        "normalize_enabled",
        "homoglyph_enabled",
        "template_enabled",
        "pinyin_enabled",
        "auto_enforce_normalized",
    ):
        settings[key] = bool(settings.get(key))
    if settings.get("mode") not in MODERATION_MODES:
        settings["mode"] = "lenient"
    if settings.get("image_review") not in IMAGE_REVIEW_MODES:
        settings["image_review"] = "off"
    if settings.get("join_review_mode") not in JOIN_REVIEW_MODES:
        settings["join_review_mode"] = "off"
    if settings.get("join_profile_missing") not in JOIN_PROFILE_MISSING_MODES:
        settings["join_profile_missing"] = "manual"
    if settings.get("join_gate_action") not in JOIN_GATE_ACTIONS:
        settings["join_gate_action"] = "decline"
    if settings.get("join_avatar_review") not in JOIN_AVATAR_REVIEW_MODES:
        settings["join_avatar_review"] = "off"
    if settings.get("join_answer_action") not in JOIN_ANSWER_ACTIONS:
        settings["join_answer_action"] = "manual"
    settings["join_expected_answer"] = str(
        settings.get("join_expected_answer") or ""
    ).strip()
    settings["join_answer_regex"] = str(settings.get("join_answer_regex") or "").strip()
    answers = settings.get("join_answer_keywords")
    if not isinstance(answers, list):
        answers = defaults.get("join_answer_keywords") or []
    settings["join_answer_keywords"] = [
        str(item).strip() for item in answers if str(item).strip()
    ]
    conditions = settings.get("send_conditions")
    if not isinstance(conditions, list):
        conditions = ["rule_hit"]
    conditions = [
        str(item)
        for item in conditions
        if str(item) in SEND_CONDITIONS or str(item).startswith(RISK_CONDITION_PREFIX)
    ]
    settings["send_conditions"] = conditions or ["rule_hit"]
    domains = settings.get("domain_allowlist")
    if not isinstance(domains, list):
        domains = defaults.get("domain_allowlist") or []
    settings["domain_allowlist"] = [
        str(item).strip() for item in domains if str(item).strip()
    ]
    if not isinstance(settings.get("mute_steps"), dict):
        settings["mute_steps"] = {"3": 600, "4": 3600, "5": 86400}
    if not isinstance(settings.get("action_matrix"), dict):
        settings["action_matrix"] = default_settings()["action_matrix"]
    for key in (
        "notify_session",
        "db_path",
        "group_rules_brief",
        "prompt_system",
        "prompt_user",
        "llm_provider_id",
    ):
        settings[key] = str(settings.get(key) or "")
    return settings


class PluginStore:
    """插件配置与状态的内存缓存 + KV 持久化。"""

    def __init__(self, kv: KVBackend, *, logger: Any = None) -> None:
        self._kv = kv
        self.logger = logger
        self._lock = asyncio.Lock()
        self._settings: dict[str, Any] = default_settings()
        self._groups: dict[str, GroupConfig] = {}
        self._keywords: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        self._templates: list[dict[str, Any]] = []
        self._homoglyph: dict[str, str] = {}
        self._trusted: dict[str, list[str]] = {}
        self._local_blacklist: dict[str, list[str]] = {}
        self._global_blacklist: dict[str, dict[str, Any]] = {}
        self._role_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._member_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._ui_state: dict[str, Any] = {}
        self._join_cursor: dict[str, dict[str, Any]] = {}
        self._profile_cache: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._loaded = False

    # ------------------------------------------------------------------
    # 加载与保存
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """从 KV 载入全部配置（首次运行时写入默认值）。"""
        raw_settings = await self._kv.get(KEY_SETTINGS, None)
        first_run = not isinstance(raw_settings, dict) or not raw_settings
        self._settings = normalize_settings(raw_settings)
        raw_groups = await self._kv.get(KEY_GROUPS, {})
        self._groups = {}
        if isinstance(raw_groups, dict):
            for key, value in raw_groups.items():
                if isinstance(value, dict):
                    value.setdefault("group_id", str(key))
                    self._groups[str(key)] = GroupConfig.from_dict(value)
        raw_keywords = await self._kv.get(KEY_KEYWORDS, {})
        if isinstance(raw_keywords, dict):
            for bucket in ("hard", "soft"):
                items = raw_keywords.get(bucket)
                if isinstance(items, list):
                    self._keywords[bucket] = [item for item in items if isinstance(item, dict)]
        for key, target in (
            (KEY_TRUSTED, self._trusted),
            (KEY_LOCAL_BLACKLIST, self._local_blacklist),
            (KEY_GLOBAL_BLACKLIST, self._global_blacklist),
            (KEY_ROLE_CACHE, self._role_cache),
            (KEY_MEMBER_CACHE, self._member_cache),
            (KEY_PROFILE_CACHE, self._profile_cache),
        ):
            raw = await self._kv.get(key, {})
            if isinstance(raw, dict):
                target.update({str(k): v for k, v in raw.items() if isinstance(v, (dict, list))})
        raw_templates = await self._kv.get(KEY_TEMPLATES, [])
        self._templates = (
            [item for item in raw_templates if isinstance(item, dict)]
            if isinstance(raw_templates, list)
            else []
        )
        raw_homoglyph = await self._kv.get(KEY_HOMOGLYPH, {})
        self._homoglyph = (
            {str(k): str(v) for k, v in raw_homoglyph.items() if str(k) and str(v)}
            if isinstance(raw_homoglyph, dict)
            else {}
        )
        raw_ui = await self._kv.get(KEY_UI_STATE, {})
        if isinstance(raw_ui, dict):
            self._ui_state = dict(raw_ui)
        self._loaded = True
        self._dirty.clear()
        if first_run:
            # 首次安装：落盘默认配置（dry_run=True / lenient）
            await self.flush(force=True)
            if self.logger is not None:
                self.logger.info("首次运行：已写入默认配置（dry_run=true, mode=lenient）")

    async def flush(self, *, force: bool = False) -> None:
        """把内存里变更过的键写回 KV。"""
        if not self._loaded:
            return
        async with self._lock:
            dirty = set(self._dirty)
            if force:
                dirty |= {KEY_SETTINGS, KEY_GROUPS}
            self._dirty.clear()
        for key in dirty:
            try:
                if key == KEY_SETTINGS:
                    await self._kv.put(KEY_SETTINGS, self._settings)
                elif key == KEY_GROUPS:
                    await self._kv.put(
                        KEY_GROUPS, {gid: cfg.to_dict() for gid, cfg in self._groups.items()}
                    )
                elif key == KEY_KEYWORDS:
                    await self._kv.put(KEY_KEYWORDS, self._keywords)
                elif key == KEY_TRUSTED:
                    await self._kv.put(KEY_TRUSTED, self._trusted)
                elif key == KEY_LOCAL_BLACKLIST:
                    await self._kv.put(KEY_LOCAL_BLACKLIST, self._local_blacklist)
                elif key == KEY_GLOBAL_BLACKLIST:
                    await self._kv.put(KEY_GLOBAL_BLACKLIST, self._global_blacklist)
                elif key == KEY_ROLE_CACHE:
                    await self._kv.put(KEY_ROLE_CACHE, self._role_cache)
                elif key == KEY_MEMBER_CACHE:
                    await self._kv.put(KEY_MEMBER_CACHE, self._member_cache)
                elif key == KEY_UI_STATE:
                    await self._kv.put(KEY_UI_STATE, self._ui_state)
                elif key == KEY_JOIN_CURSOR:
                    await self._kv.put(KEY_JOIN_CURSOR, self._join_cursor)
                elif key == KEY_TEMPLATES:
                    await self._kv.put(KEY_TEMPLATES, self._templates)
                elif key == KEY_HOMOGLYPH:
                    await self._kv.put(KEY_HOMOGLYPH, self._homoglyph)
                elif key == KEY_PROFILE_CACHE:
                    await self._kv.put(KEY_PROFILE_CACHE, self._profile_cache)
            except Exception as exc:  # pragma: no cover - KV 失败不应中断业务
                self._dirty.add(key)
                if self.logger is not None:
                    self.logger.error("写入 KV %s 失败：%s", key, exc)

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        """返回配置副本（调用方不应直接修改）。"""
        return copy.deepcopy(self._settings)

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def dry_run(self) -> bool:
        return bool(self._settings.get("dry_run", True))

    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """按 patch 更新配置并持久化，返回归一化后的完整配置。"""
        merged = dict(self._settings)
        merged.update({k: v for k, v in (patch or {}).items() if k in merged})
        self._settings = normalize_settings(merged)
        self._dirty.add(KEY_SETTINGS)
        await self.flush()
        return self.settings()

    # ------------------------------------------------------------------
    # 群
    # ------------------------------------------------------------------
    def groups(self) -> dict[str, GroupConfig]:
        return self._groups

    def group(self, group_id: str) -> GroupConfig | None:
        return self._groups.get(group_id)

    def group_or_default(self, group_id: str) -> GroupConfig:
        return self._groups.get(group_id) or GroupConfig(group_id=group_id)

    async def ensure_group(
        self,
        group_id: str,
        *,
        name: str = "",
        source: str = "auto",
        platform_id: str = "",
    ) -> GroupConfig:
        """确保群记录存在（不存在则新建），返回该记录。"""
        config = self._groups.get(group_id)
        if config is None:
            config = GroupConfig(
                group_id=group_id,
                platform_id=str(platform_id or ""),
                name=name,
                added_at=now_ts(),
                last_seen=now_ts(),
                source=source,
                # 刻意留空：空 = 跟随全局模式。早期版本会把当时的全局默认值复制进来，
                # 导致之后在管理台改「默认模式」对已登记的群完全不起作用。
                mode="",
                join_review_mode="",
            )
            self._groups[group_id] = config
        else:
            if name and config.name != name:
                config.name = name
            if platform_id and config.platform_id != platform_id:
                config.platform_id = str(platform_id)
        self._dirty.add(KEY_GROUPS)
        return config

    async def touch_group(
        self, group_id: str, *, name: str = "", platform_id: str = ""
    ) -> None:
        """消息到达时登记活跃群（只更新内存，由调度器批量落盘）。"""
        if not group_id:
            return
        await self.ensure_group(
            group_id, name=name, source="auto", platform_id=platform_id
        )
        self._groups[group_id].last_seen = now_ts()

    async def update_group(self, group_id: str, patch: dict[str, Any]) -> GroupConfig:
        """更新单个群的配置。"""
        config = await self.ensure_group(group_id)
        data = config.to_dict()
        for key, value in (patch or {}).items():
            if key in data and key != "group_id":
                data[key] = value
        updated = GroupConfig.from_dict(data)
        self._groups[group_id] = updated
        self._dirty.add(KEY_GROUPS)
        await self.flush()
        return updated

    async def remove_group(self, group_id: str) -> bool:
        """删除插件侧的群记录（不影响平台侧）。"""
        existed = self._groups.pop(group_id, None) is not None
        if existed:
            self._dirty.add(KEY_GROUPS)
            await self.flush()
        return existed

    async def set_capabilities(
        self, group_id: str, results: dict[str, CapabilityResult]
    ) -> GroupConfig:
        """保存一次能力探测结果。"""
        config = await self.ensure_group(group_id)
        config.capabilities = {name: result.to_dict() for name, result in results.items()}
        self._dirty.add(KEY_GROUPS)
        return config

    # ------------------------------------------------------------------
    # 关键词 / 信任名单 / 黑名单
    # ------------------------------------------------------------------
    def templates(self) -> list[dict[str, Any]]:
        """广告模板（KV: templates；为空时由规则引擎使用内置模板）。"""
        return self._templates

    async def update_templates(self, payload: Any) -> list[dict[str, Any]]:
        items = [item for item in (payload or []) if isinstance(item, dict)]
        self._templates = items
        self._dirty.add(KEY_TEMPLATES)
        await self.flush()
        return items

    def homoglyph(self) -> dict[str, str]:
        """形近字表（KV: homoglyph；为空时使用内置基线）。"""
        return self._homoglyph

    async def update_homoglyph(self, payload: Any) -> dict[str, str]:
        items = {
            str(k): str(v)
            for k, v in (payload or {}).items()
            if str(k) and str(v) and str(k) != str(v)
        }
        self._homoglyph = items
        self._dirty.add(KEY_HOMOGLYPH)
        await self.flush()
        return items

    def keywords(self) -> dict[str, list[dict[str, Any]]]:
        return self._keywords

    async def update_keywords(self, keywords: dict[str, list[dict[str, Any]]]) -> None:
        for bucket in ("hard", "soft"):
            items = keywords.get(bucket)
            if isinstance(items, list):
                self._keywords[bucket] = [item for item in items if isinstance(item, dict)]
        self._dirty.add(KEY_KEYWORDS)
        await self.flush()

    def trusted(self, group_id: str) -> list[str]:
        return list(self._trusted.get(group_id, []))

    async def update_trusted(self, group_id: str, members: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in members if str(item).strip()]
        self._trusted[group_id] = cleaned
        self._dirty.add(KEY_TRUSTED)
        await self.flush()
        return cleaned

    # ------------------------------------------------------------------
    # 跨群黑名单（B5）：与群级黑名单分开存，默认只拒绝入群，不自动移出成员
    # ------------------------------------------------------------------
    def global_blacklist(self) -> dict[str, dict[str, Any]]:
        """返回 {openid: {"reason","added_by","added_at"}}。"""
        return {str(key): dict(value) for key, value in self._global_blacklist.items()}

    def is_globally_blacklisted(self, openid: str) -> bool:
        return str(openid or "").strip() in self._global_blacklist

    def global_blacklist_reason(self, openid: str) -> str:
        entry = self._global_blacklist.get(str(openid or "").strip()) or {}
        return str(entry.get("reason") or "")

    async def add_global_blacklist(
        self, openid: str, *, reason: str = "", added_by: str = ""
    ) -> dict[str, dict[str, Any]]:
        key = str(openid or "").strip()
        if not key:
            return self.global_blacklist()
        self._global_blacklist[key] = {
            "reason": str(reason or "")[:200],
            "added_by": str(added_by or ""),
            "added_at": now_ts(),
        }
        self._dirty.add(KEY_GLOBAL_BLACKLIST)
        await self.flush()
        return self.global_blacklist()

    async def remove_global_blacklist(self, openid: str) -> bool:
        key = str(openid or "").strip()
        if key not in self._global_blacklist:
            return False
        self._global_blacklist.pop(key, None)
        self._dirty.add(KEY_GLOBAL_BLACKLIST)
        await self.flush()
        return True

    def local_blacklist(self, group_id: str) -> list[str]:
        return list(self._local_blacklist.get(group_id, []))

    async def update_local_blacklist(self, group_id: str, members: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in members if str(item).strip()]
        self._local_blacklist[group_id] = cleaned
        self._dirty.add(KEY_LOCAL_BLACKLIST)
        await self.flush()
        return cleaned

    # ------------------------------------------------------------------
    # 入群申请轮询游标
    # ------------------------------------------------------------------
    def get_join_cursor_sync(self, group_id: str) -> str:
        entry = self._join_cursor.get(group_id) or {}
        return str(entry.get("cursor") or "")

    async def get_join_cursor(self, group_id: str) -> str:
        """读取该群的入群申请分页游标（空串表示从头拉取）。"""
        return self.get_join_cursor_sync(group_id)

    async def set_join_cursor(self, group_id: str, cursor: str) -> None:
        """保存分页游标。"""
        self._join_cursor[group_id] = {"cursor": str(cursor or ""), "at": now_ts()}
        self._dirty.add(KEY_JOIN_CURSOR)
        await self.flush()

    # ------------------------------------------------------------------
    # 申请人画像缓存
    # ------------------------------------------------------------------
    def profile_cache(self) -> dict[str, dict[str, Any]]:
        """返回画像缓存副本。"""
        return copy.deepcopy(self._profile_cache)

    def get_profile(self, cache_key: str) -> dict[str, Any] | None:
        entry = self._profile_cache.get(str(cache_key))
        return dict(entry) if isinstance(entry, dict) else None

    async def put_profile(self, cache_key: str, payload: dict[str, Any]) -> None:
        """写入画像缓存（只应由「成功画像」调用）。"""
        if not cache_key or not isinstance(payload, dict):
            return
        self._profile_cache[str(cache_key)] = dict(payload)
        self._dirty.add(KEY_PROFILE_CACHE)

    async def drop_expired_profiles(self, ttl_days: int) -> int:
        """清理过期画像（ttl_days<=0 表示不清理），返回清理条数。"""
        if ttl_days <= 0:
            return 0
        deadline = now_ts() - int(ttl_days) * 86400
        stale = [
            key
            for key, value in self._profile_cache.items()
            if not isinstance(value, dict) or int(value.get("fetched_at") or 0) < deadline
        ]
        for key in stale:
            self._profile_cache.pop(key, None)
        if stale:
            self._dirty.add(KEY_PROFILE_CACHE)
            await self.flush()
        return len(stale)

    # ------------------------------------------------------------------
    # 成员与角色缓存
    # ------------------------------------------------------------------
    async def remember_member(
        self, group_id: str, member_openid: str, *, name: str = "", role: str = ""
    ) -> None:
        """记录群成员昵称/角色（M1 用于"按昵称操作"与权限判定）。"""
        if not (group_id and member_openid):
            return
        now = now_ts()
        cache = self._member_cache.setdefault(group_id, {})
        entry = cache.setdefault(member_openid, {})
        if name:
            entry["name"] = name
        # 首次见到即记录 first_seen，用于"入群时长/新成员"判定
        entry.setdefault("first_seen", now)
        entry["seen_unix"] = now
        if role:
            self._role_cache.setdefault(group_id, {})[member_openid] = {
                "role": role,
                "seen_unix": now,
            }
            self._dirty.add(KEY_ROLE_CACHE)
        if len(cache) > MEMBER_CACHE_PER_GROUP:
            oldest = sorted(cache.items(), key=lambda item: item[1].get("seen_unix", 0))
            for key, _ in oldest[: len(cache) - MEMBER_CACHE_PER_GROUP]:
                cache.pop(key, None)
        self._dirty.add(KEY_MEMBER_CACHE)

    def member_first_seen(self, group_id: str, member_openid: str) -> int | None:
        """该成员在本群首次被看到的时间戳（未知时返回 None）。"""
        entry = self._member_cache.get(group_id, {}).get(member_openid, {})
        value = entry.get("first_seen")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def member_name(self, group_id: str, member_openid: str) -> str:
        entry = self._member_cache.get(group_id, {}).get(member_openid, {})
        return str(entry.get("name") or "")

    def member_role(self, group_id: str, member_openid: str) -> str:
        entry = self._role_cache.get(group_id, {}).get(member_openid, {})
        if not entry:
            return ""
        if now_ts() - int(entry.get("seen_unix") or 0) > ROLE_CACHE_TTL:
            return ""
        return str(entry.get("role") or "")

    def find_member_by_name(self, group_id: str, keyword: str) -> list[tuple[str, str]]:
        """按昵称关键字查本地缓存，返回 [(openid, name)]。"""
        needle = (keyword or "").strip().lstrip("@")
        if not needle:
            return []
        result: list[tuple[str, str]] = []
        for openid, entry in self._member_cache.get(group_id, {}).items():
            name = str(entry.get("name") or "")
            if needle and (needle == name or needle in name):
                result.append((openid, name))
        return result[:20]

    def is_group_admin(self, group_id: str, member_openid: str) -> bool:
        return self.member_role(group_id, member_openid) in {"admin", "owner"}

    # ------------------------------------------------------------------
    # UI 状态
    # ------------------------------------------------------------------
    def ui_state(self) -> dict[str, Any]:
        return dict(self._ui_state)

    async def update_ui_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        self._ui_state.update(patch or {})
        self._dirty.add(KEY_UI_STATE)
        await self.flush()
        return dict(self._ui_state)
