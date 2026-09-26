"""WebUI 后端接口（REST + SSE）。

路由全部注册在 /{plugin_name}/... 下；前端通过 window.AstrBotPluginPage
的 apiGet/apiPost/subscribeSSE 调用（endpoint 不带插件名与前导斜杠）。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request, stream_response

from .audit import LOG_TABLES
from .models import (
    CAPABILITIES,
    CAPABILITY_LABELS,
    JOIN_REVIEW_MODES,
    MODERATION_MODES,
    SEND_CONDITIONS,
    default_settings,
)
from .utils import now_ts

PLUGIN_NAME = "astrbot_plugin_qq_group_manager"
SSE_HEARTBEAT = 15.0
MAX_SSE_QUEUE = 200


class EventBus:
    """进程内 SSE 广播总线（每个 topic 一个订阅者队列集合）。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_SSE_QUEUE)
        self._subscribers.setdefault(topic, set()).add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        bucket = self._subscribers.get(topic)
        if bucket and queue in bucket:
            bucket.discard(queue)
        if bucket is not None and not bucket:
            self._subscribers.pop(topic, None)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(topic, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:  # 慢消费者：丢弃最旧的一条
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def subscriber_count(self, topic: str | None = None) -> int:
        if topic is None:
            return sum(len(bucket) for bucket in self._subscribers.values())
        return len(self._subscribers.get(topic, ()))


class WebApi:
    """把插件服务暴露为 Web 接口。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self) -> None:
        """注册全部路由（在插件 __init__ 中调用）。"""
        routes: list[tuple[str, Any, list[str], str]] = [
            (f"/{PLUGIN_NAME}/config", self.config_get, ["GET"], "读取插件配置"),
            (f"/{PLUGIN_NAME}/config", self.config_set, ["POST"], "保存插件配置"),
            (f"/{PLUGIN_NAME}/summary", self.summary, ["GET"], "总览统计"),
            (f"/{PLUGIN_NAME}/groups", self.groups, ["GET"], "群列表与能力矩阵"),
            (f"/{PLUGIN_NAME}/groups/probe", self.group_probe, ["POST"], "能力探测"),
            (
                f"/{PLUGIN_NAME}/groups/refresh-names",
                self.group_refresh_names,
                ["POST"],
                "刷新群名称",
            ),
            (
                f"/{PLUGIN_NAME}/groups/moderation",
                self.group_moderation,
                ["POST"],
                "启用/停用某群审核",
            ),
            (f"/{PLUGIN_NAME}/groups/add", self.group_add, ["POST"], "手动添加群"),
            (
                f"/{PLUGIN_NAME}/groups/mode",
                self.group_mode,
                ["POST"],
                "设置某群审核模式（空=跟随全局）",
            ),
            (
                f"/{PLUGIN_NAME}/groups/join_mode",
                self.group_join_mode,
                ["POST"],
                "设置某群入群审批模式",
            ),
            (f"/{PLUGIN_NAME}/groups/remove", self.group_remove, ["POST"], "移除群记录"),
            (f"/{PLUGIN_NAME}/selfcheck", self.selfcheck, ["POST"], "全量能力自检"),
            (f"/{PLUGIN_NAME}/instructions", self.instructions, ["GET"], "指令速查"),
            (f"/{PLUGIN_NAME}/ui_state", self.ui_state_set, ["POST"], "保存界面状态"),
            (f"/{PLUGIN_NAME}/db/info", self.db_info, ["GET"], "审计库信息"),
            (f"/{PLUGIN_NAME}/db/maintain", self.db_maintain, ["POST"], "审计库维护"),
            (f"/{PLUGIN_NAME}/logs/clear", self.logs_clear, ["POST"], "清空日志"),
            (f"/{PLUGIN_NAME}/logs/export", self.logs_export, ["GET"], "导出日志"),
            (f"/{PLUGIN_NAME}/events/stream", self.events_stream, ["GET"], "实时日志流(SSE)"),
            (f"/{PLUGIN_NAME}/dryrun", self.dryrun, ["POST"], "审核链路试跑（不执行动作）"),
            (f"/{PLUGIN_NAME}/rules/test", self.rules_test, ["POST"], "本地规则命中测试"),
            (f"/{PLUGIN_NAME}/mutes", self.mutes, ["GET"], "禁言台账"),
            (f"/{PLUGIN_NAME}/mutes/unmute", self.mutes_unmute, ["POST"], "批量解禁"),
            (f"/{PLUGIN_NAME}/mutes/mute", self.mutes_mute, ["POST"], "批量禁言"),
            (f"/{PLUGIN_NAME}/mutes/sync", self.mutes_sync, ["POST"], "与平台对账禁言"),
            (f"/{PLUGIN_NAME}/members/search", self.members_search, ["GET"], "成员查询"),
            (f"/{PLUGIN_NAME}/members/remove", self.members_remove, ["POST"], "批量移除成员"),
            (f"/{PLUGIN_NAME}/blacklist", self.blacklist_get, ["GET"], "黑名单查询"),
            (f"/{PLUGIN_NAME}/blacklist", self.blacklist_set, ["POST"], "黑名单增删"),
            (
                f"/{PLUGIN_NAME}/global_blacklist",
                self.global_blacklist_get,
                ["GET"],
                "跨群黑名单列表",
            ),
            (
                f"/{PLUGIN_NAME}/global_blacklist/update",
                self.global_blacklist_update,
                ["POST"],
                "跨群黑名单增删",
            ),
            (f"/{PLUGIN_NAME}/joins", self.joins_get, ["GET"], "入群申请列表"),
            (f"/{PLUGIN_NAME}/joins/fetch", self.joins_fetch, ["POST"], "立即拉取入群申请"),
            (f"/{PLUGIN_NAME}/joins/decide", self.joins_decide, ["POST"], "人工审批入群申请"),
            (f"/{PLUGIN_NAME}/joins/settings", self.joins_settings, ["POST"], "保存入群审批配置"),
            (f"/{PLUGIN_NAME}/appeals", self.appeals_get, ["GET"], "申诉列表"),
            (f"/{PLUGIN_NAME}/appeals/decide", self.appeals_decide, ["POST"], "处理申诉"),
            (
                f"/{PLUGIN_NAME}/appeal_whitelist",
                self.appeal_whitelist_get,
                ["GET"],
                "申诉白名单列表",
            ),
            (
                f"/{PLUGIN_NAME}/appeal_whitelist",
                self.appeal_whitelist_post,
                ["POST"],
                "申诉白名单增删",
            ),
            (f"/{PLUGIN_NAME}/policy", self.policy_get, ["GET"], "官方入群审核策略"),
            (f"/{PLUGIN_NAME}/policy", self.policy_post, ["POST"], "策略维护"),
        ]
        for kind in LOG_TABLES:
            routes.append(
                (
                    f"/{PLUGIN_NAME}/logs/{kind}",
                    self._logs_handler(kind),
                    ["GET"],
                    f"查询{kind}日志",
                )
            )
        for route, handler, methods, desc in routes:
            try:
                self.service.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # pragma: no cover
                logger.error("注册 Web API %s 失败：%s", route, exc)
        logger.debug("QQ群管理：已注册 %d 个 Web API 路由", len(routes))

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _service_ready(self) -> bool:
        return bool(getattr(self.service, "initialized", False))

    @staticmethod
    def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
        value = request.query.get(name, default, type=int)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _filters_from_query() -> dict[str, Any]:
        filters: dict[str, Any] = {}
        for key in (
            "group_id",
            "verdict",
            "category",
            "action",
            "capability",
            "caller",
            "source",
            "keyword",
        ):
            value = request.query.get(key)
            if value:
                filters[key] = value
        for key in ("ok", "dry_run", "appealed"):
            value = request.query.get(key)
            if value not in (None, ""):
                filters[key] = 1 if str(value).lower() in {"1", "true", "yes"} else 0
        days = request.query.get("days")
        if days:
            try:
                filters["ts_from"] = now_ts() - max(1, int(days)) * 86400
            except ValueError:
                pass
        ts_from = request.query.get("ts_from")
        if ts_from:
            try:
                filters["ts_from"] = int(ts_from)
            except ValueError:
                pass
        return filters

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    async def config_get(self):
        """返回配置 + 群列表 + 运行态（WebUI 首屏）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        store = self.service.store
        return json_response(
            {
                "settings": store.settings(),
                "groups": self.service.groups_snapshot(),
                "keywords": store.keywords(),
                "templates": store.templates(),
                "homoglyph": store.homoglyph(),
                "ui_state": store.ui_state(),
                "runtime": self.service.runtime_status(),
                "providers": self.service.list_providers(),
                "options": {
                    "modes": list(MODERATION_MODES),
                    "join_modes": list(JOIN_REVIEW_MODES),
                    "send_conditions": list(SEND_CONDITIONS),
                    "capabilities": [
                        {"key": key, "label": CAPABILITY_LABELS.get(key, key)}
                        for key in CAPABILITIES
                    ],
                    "default_settings": default_settings(),
                },
            }
        )

    async def config_set(self):
        """保存配置：{section: settings|keywords|ui_state, data: {...}}。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        section = str(payload.get("section") or "settings")
        data = payload.get("data")
        if not isinstance(data, dict):
            return error_response("data 必须是对象")
        store = self.service.store
        try:
            if section == "settings":
                settings = await store.update_settings(data)
                self.service.reload_rules()
                return json_response({"section": section, "settings": settings})
            if section == "templates":
                items = await self.service.update_templates(data)
                return json_response({"section": section, "templates": items})
            if section == "homoglyph":
                items = await self.service.update_homoglyph(data)
                return json_response({"section": section, "homoglyph": items})
            if section == "keywords":
                # 必须走 service：它会在写库后 hot-reload 规则引擎，
                # 否则 WebUI 里改的动作（例如给硬规则加"禁言"）要等插件重载才生效。
                keywords = await self.service.update_keywords(data)
                return json_response({"section": section, "keywords": keywords})
            if section == "ui_state":
                state = await store.update_ui_state(data)
                return json_response({"section": section, "ui_state": state})
        except Exception as exc:
            logger.error("保存配置失败：%s", exc, exc_info=True)
            return error_response(f"保存失败：{exc}")
        return error_response(f"未知配置分区：{section}")

    async def ui_state_set(self):
        """单独保存界面记忆项。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        state = await self.service.store.update_ui_state(payload)
        return json_response({"ui_state": state})

    # ------------------------------------------------------------------
    # 总览 / 群
    # ------------------------------------------------------------------
    async def summary(self):
        """总览统计（今日/近 N 天）+ 任务状态 + 运行态。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        days = self._int_arg("days", 1, 1, 90)
        audit = self.service.audit
        stats = await audit.summary(days) if audit is not None else {}
        return json_response(
            {
                "runtime": self.service.runtime_status(),
                "stats": stats,
                "tasks": self.service.scheduler.states(),
                "db": await audit.db_info() if audit is not None else {},
            }
        )

    async def groups(self):
        """群列表（含能力矩阵）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        return json_response({"groups": self.service.groups_snapshot()})

    async def group_refresh_names(self):
        """补齐缺失的群名称（官方走开放接口，OneBot 走 get_group_info）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        try:
            limit = int((payload or {}).get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(200, limit))
        try:
            result = await self.service.refresh_group_names(limit=limit)
        except Exception as exc:
            return error_response(f"刷新群名失败：{exc}")
        return json_response({**result, "groups": self.service.groups_snapshot()})

    async def group_probe(self):
        """探测单群或全部群的能力。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        probe_all = bool((payload or {}).get("all"))
        targets = [group_id] if group_id else list(self.service.store.groups())
        if not targets:
            return error_response("暂无可探测的群：请先在群内发一条消息，或手动添加群")
        if not group_id and not probe_all and len(targets) > 5:
            return error_response("群数量较多，请指定 group_id 或设置 all=true")
        results: dict[str, Any] = {}
        for target in targets:
            probe = await self.service.probe_group(target, caller="webui")
            results[target] = {name: result.to_dict() for name, result in probe.items()}
        return json_response({"results": results, "groups": self.service.groups_snapshot()})

    async def group_moderation(self):
        """启用/停用某群审核（启用走前置校验：需已开启接收全部消息）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        enable = bool((payload or {}).get("enable"))
        result = await self.service.set_moderation(group_id, enable, caller="webui")
        if not result.get("ok"):
            return error_response(
                str(result.get("message") or "启用失败"),
                data={"reason_code": result.get("reason_code"), **result},
            )
        return json_response(result)

    async def group_mode(self):
        """设置某群的审核模式；mode 传空串表示"跟随全局"。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        mode = str((payload or {}).get("mode") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        if mode and mode not in MODERATION_MODES:
            return error_response(
                f"mode 必须是 {', '.join(MODERATION_MODES)} 之一，或留空表示跟随全局"
            )
        config = await self.service.store.update_group(group_id, {"mode": mode})
        return json_response({"group": config.to_dict(), "groups": self.service.groups_snapshot()})

    async def group_join_mode(self):
        """设置某群的入群审批模式（off/strict/standard/human）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        mode = str((payload or {}).get("mode") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        if mode not in JOIN_REVIEW_MODES:
            return error_response(f"mode 必须是 {', '.join(JOIN_REVIEW_MODES)} 之一")
        config = await self.service.store.update_group(group_id, {"join_review_mode": mode})
        return json_response({"group": config.to_dict(), "groups": self.service.groups_snapshot()})

    async def group_add(self):
        """手动添加群记录（群列表自动登记之外的手动入口）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        name = str((payload or {}).get("name") or "").strip()
        await self.service.store.ensure_group(group_id, name=name, source="manual")
        await self.service.store.flush()
        return json_response({"groups": self.service.groups_snapshot()})

    async def group_remove(self):
        """移除插件侧的群记录（不影响平台侧）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        removed = await self.service.store.remove_group(group_id)
        return json_response({"removed": removed, "groups": self.service.groups_snapshot()})

    async def selfcheck(self):
        """全量能力自检。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "").strip()
        targets = [group_id] if group_id else list(self.service.store.groups())
        if not targets:
            return error_response("暂无可自检的群")
        report: dict[str, Any] = {}
        for target in targets:
            probe = await self.service.probe_group(target, caller="selfcheck")
            report[target] = {
                "capabilities": {name: result.to_dict() for name, result in probe.items()},
                "suggestions": self.service.suggestions(target, probe),
            }
        return json_response({"report": report, "transport": self.service.transport_status()})

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _logs_handler(self, kind: str):
        async def handler():
            if not self._service_ready():
                return error_response("插件尚未初始化完成，请稍后重试")
            audit = self.service.audit
            if audit is None:
                return error_response("审计库不可用")
            page = self._int_arg("page", 1, 1, 10000)
            page_size = self._int_arg("page_size", 20, 1, 200)
            order = request.query.get("order") or "ts_unix DESC"
            result = await audit.query_logs(
                kind,
                filters=self._filters_from_query(),
                page=page,
                page_size=page_size,
                order=order,
            )
            return json_response({"kind": kind, **result})

        return handler

    async def logs_clear(self):
        """清空指定范围日志（危险操作：前端需二次确认）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        scope = str((payload or {}).get("scope") or "all")
        before_days = (payload or {}).get("before_days")
        if scope not in (*LOG_TABLES.keys(), "all"):
            return error_response(f"未知范围：{scope}")
        deleted = await self.service.audit.clear(
            scope, before_days=int(before_days) if before_days is not None else None
        )
        logger.warning("管理员 %s 清空日志：scope=%s deleted=%s", request.username, scope, deleted)
        return json_response({"deleted": deleted})

    async def logs_export(self):
        """导出当前筛选结果（CSV / JSON）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        kind = str(request.query.get("kind") or "events")
        if kind not in LOG_TABLES:
            return error_response(f"未知日志类型：{kind}")
        fmt = str(request.query.get("format") or "csv").lower()
        limit = self._int_arg("limit", 2000, 1, 20000)
        result = await self.service.audit.query_logs(
            kind,
            filters=self._filters_from_query(),
            page=1,
            page_size=limit,
        )
        items = result.get("items", [])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = (
            self.service.data_dir / f"export-{kind}-{stamp}.{'json' if fmt == 'json' else 'csv'}"
        )
        if fmt == "json":
            target.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            buffer = io.StringIO()
            if items:
                writer = csv.DictWriter(buffer, fieldnames=list(items[0].keys()))
                writer.writeheader()
                writer.writerows(items)
            target.write_text(buffer.getvalue(), encoding="utf-8-sig")
        return file_response(target, filename=target.name)

    async def events_stream(self):
        """SSE 实时日志流。"""
        topic = str(request.query.get("topic") or "audit")
        bus: EventBus = self.service.bus
        queue = bus.subscribe(topic)

        async def generator():
            yield ": connected\n\n"
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            except asyncio.CancelledError:  # pragma: no cover - 客户端断开
                raise
            finally:
                bus.unsubscribe(topic, queue)

        return stream_response(generator(), headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------
    # 数据库 / 其他
    # ------------------------------------------------------------------
    async def db_info(self):
        """审计库体积、行数、保留策略。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        settings = self.service.store.settings()
        return json_response(
            {
                "db": await self.service.audit.db_info(),
                "retention": {
                    "events": settings.get("retention_events_days"),
                    "api": settings.get("retention_api_days"),
                    "capability": settings.get("retention_capability_days"),
                    "join": settings.get("retention_join_days"),
                },
                "queue": dict(self.service.audit.stats),
            }
        )

    async def db_maintain(self):
        """审计库维护：prune / vacuum / backup。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        op = str((payload or {}).get("op") or "").lower()
        audit = self.service.audit
        settings = self.service.store.settings()
        if op == "prune":
            deleted = await audit.prune(
                {
                    "events": settings["retention_events_days"],
                    "actions": settings["retention_events_days"],
                    "api": settings["retention_api_days"],
                    "capability": settings["retention_capability_days"],
                    "join": settings["retention_join_days"],
                }
            )
            return json_response({"deleted": deleted, "db": await audit.db_info()})
        if op == "vacuum":
            await audit.vacuum()
            return json_response({"db": await audit.db_info()})
        if op == "backup":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = self.service.data_dir / f"backup-{stamp}.db"
            await audit.backup(target)
            return file_response(target, filename=target.name)
        return error_response(f"未知维护操作：{op}")

    # ------------------------------------------------------------------
    # 审核 / 规则 / 成员 / 入群
    # ------------------------------------------------------------------
    async def dryrun(self):
        """审核链路试跑（完整判定，不执行任何动作）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        try:
            result = await self.service.dryrun(payload)
        except Exception as exc:
            logger.error("试跑失败：%s", exc, exc_info=True)
            return error_response(f"试跑失败：{exc}")
        return json_response(result)

    async def rules_test(self):
        """本地规则命中测试。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        text = str((payload or {}).get("text") or "")
        group_id = str((payload or {}).get("group_id") or "")
        return json_response(await self.service.test_rules(text, group_id))

    async def mutes(self):
        """禁言台账。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        group_id = str(request.query.get("group_id") or "").strip()
        rows = await self.service.list_mutes(group_id or None)
        return json_response({"items": rows, "total": len(rows)})

    async def mutes_unmute(self):
        """批量解禁。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        openids = [str(item) for item in ((payload or {}).get("member_openids") or [])]
        if not group_id or not openids:
            return error_response("缺少 group_id 或 member_openids")
        return json_response(await self.service.unmute_members(group_id, openids))

    async def mutes_mute(self):
        """批量禁言。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        openids = [str(item) for item in ((payload or {}).get("member_openids") or [])]
        seconds = self._int_arg("seconds", 0, 0, 0) or int((payload or {}).get("seconds") or 600)
        if not group_id or not openids:
            return error_response("缺少 group_id 或 member_openids")
        return json_response(
            await self.service.mute_members(
                group_id, openids, seconds=int(seconds), reason="WebUI 操作"
            )
        )

    async def mutes_sync(self):
        """与平台对账禁言状态。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        if not group_id:
            return error_response("缺少 group_id")
        return json_response(await self.service.sync_mutes(group_id))

    async def members_search(self):
        """成员查询（本地缓存 + 平台接口）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        group_id = str(request.query.get("group_id") or "")
        query = str(request.query.get("q") or "")
        if not group_id:
            return error_response("缺少 group_id")
        return json_response(await self.service.member_search(group_id, query))

    async def members_remove(self):
        """批量移除成员（内邀能力）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        openids = [str(item) for item in ((payload or {}).get("member_openids") or [])]
        if not group_id or not openids:
            return error_response("缺少 group_id 或 member_openids")
        result = await self.service.remove_members(
            group_id,
            openids,
            add_to_blacklist=bool((payload or {}).get("add_to_blacklist")),
        )
        if not result.get("ok"):
            return error_response(str(result.get("message") or "移除失败"), data=result)
        return json_response(result)

    async def blacklist_get(self):
        """黑名单查询。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        group_id = str(request.query.get("group_id") or "")
        if not group_id:
            return error_response("缺少 group_id")
        return json_response(await self.service.blacklist_snapshot(group_id))

    async def blacklist_set(self):
        """黑名单增删（op=add/del）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        op = str((payload or {}).get("op") or "add")
        openids = [str(item) for item in ((payload or {}).get("member_openids") or [])]
        if not group_id or not openids:
            return error_response("缺少 group_id 或 member_openids")
        if op not in ("add", "del"):
            return error_response("op 只能是 add 或 del")
        return json_response(
            await self.service.blacklist_update(
                group_id,
                op=op,
                openids=openids,
                local_only=bool((payload or {}).get("local_only")),
            )
        )

    async def global_blacklist_get(self):
        """跨群黑名单快照（B5）。"""
        if not self._service_ready():
            return error_response("插件尚未就绪")
        return json_response(self.service.global_blacklist_snapshot())

    async def global_blacklist_update(self):
        """新增/移除跨群黑名单条目。"""
        if not self._service_ready():
            return error_response("插件尚未就绪")
        payload = await request.json(default={})
        op = str((payload or {}).get("op") or "add")
        openid = str((payload or {}).get("openid") or "").strip()
        reason = str((payload or {}).get("reason") or "")
        if op not in ("add", "remove"):
            return error_response("op 只能是 add 或 remove")
        if not openid:
            return error_response("缺少 openid")
        result = await self.service.global_blacklist_update(
            op=op,
            openid=openid,
            reason=reason,
            by=f"webui:{request.username or 'unknown'}",
        )
        if not result.get("ok"):
            return error_response(str(result.get("error") or "操作失败"), data=result)
        return json_response(result)

    async def joins_get(self):
        """入群申请（待审 + 历史 + 策略冲突）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        group_id = str(request.query.get("group_id") or "").strip()
        return json_response(await self.service.joins_snapshot(group_id or None))

    async def joins_fetch(self):
        """立即拉取入群申请。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        if not group_id:
            return error_response("缺少 group_id")
        result = await self.service.joins_fetch(group_id)
        if not result.get("ok"):
            return error_response(str(result.get("message") or "拉取失败"), data=result)
        return json_response(result)

    async def joins_decide(self):
        """人工审批入群申请。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        group_id = str((payload or {}).get("group_id") or "")
        member_openid = str((payload or {}).get("member_openid") or "")
        op = str((payload or {}).get("op") or "approve")
        if not group_id or not member_openid:
            return error_response("缺少 group_id 或 member_openid")
        if op not in ("approve", "decline"):
            return error_response("op 只能是 approve 或 decline")
        result = await self.service.joins_decide(
            group_id,
            member_openid,
            op=op,
            join_request_id=str((payload or {}).get("join_request_id") or ""),
            reason=str((payload or {}).get("reason") or ""),
            blacklist=bool((payload or {}).get("blacklist")),
            by=f"webui:{request.username or 'unknown'}",
        )
        if not result.get("ok"):
            return error_response(str(result.get("message") or "审批失败"), data=result)
        return json_response(result)

    async def joins_settings(self):
        """保存入群审批相关配置（画像 / 门槛 / 全局模式）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        allowed = (
            "join_review_mode",
            "join_poll_interval",
            "join_min_confidence",
            "join_decline_blacklist",
            "join_trust_inviter",
            "join_profile_enabled",
            "join_min_account_days",
            "join_min_qq_level",
            "join_require_qid",
            "join_gate_action",
            "join_profile_missing",
            "join_avatar_review",
            "join_avatar_only_below",
            "join_profile_cache_days",
            "join_profile_qpm",
            "join_profile_concurrency",
            "join_expected_answer",
            "join_answer_keywords",
            "join_answer_regex",
            "join_answer_action",
            "join_answer_case_sensitive",
        )
        patch_data = {key: payload[key] for key in allowed if key in payload}
        settings = await self.service.store.update_settings(patch_data)
        return json_response({"settings": {key: settings.get(key) for key in allowed}})

    async def appeals_get(self):
        """申诉列表（state/group_id/days 筛选）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        state = str(request.query.get("state") or "pending").strip()
        group_id = str(request.query.get("group_id") or "").strip()
        days = self._int_arg("days", 30, 1, 365)
        limit = self._int_arg("limit", 100, 1, 500)
        return json_response(
            await self.service.appeals_snapshot(
                state=state or "pending",
                group_id=group_id,
                days=days,
                limit=limit,
            )
        )

    async def appeals_decide(self):
        """处理申诉（op=accept|reject），通过时执行解禁补偿。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        try:
            event_id = int((payload or {}).get("event_id") or 0)
        except (TypeError, ValueError):
            event_id = 0
        if not event_id:
            return error_response("缺少 event_id")
        op = str((payload or {}).get("op") or "accept")
        if op not in ("accept", "reject"):
            return error_response("op 只能是 accept 或 reject")
        result = await self.service.appeal_decide(
            event_id,
            accepted=op == "accept",
            note=str((payload or {}).get("note") or ""),
            by=f"webui:{request.username or 'unknown'}",
        )
        if not result.get("ok"):
            return error_response(str(result.get("message") or "处理失败"), data=result)
        return json_response(result)

    async def appeal_whitelist_get(self):
        """误判自学习白名单列表。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        return json_response(await self.service.appeal_whitelist_snapshot())

    async def appeal_whitelist_post(self):
        """白名单增删（op=add|del）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        op = str((payload or {}).get("op") or "del")
        digest = str((payload or {}).get("digest") or "")
        if op not in ("add", "del"):
            return error_response("op 只能是 add 或 del")
        if not digest:
            return error_response("缺少 digest")
        result = await self.service.appeal_whitelist_update(
            op=op,
            digest=digest,
            by=f"webui:{request.username or 'unknown'}",
            reason=str((payload or {}).get("reason") or ""),
        )
        if not result.get("ok"):
            return error_response(str(result.get("message") or "操作失败"), data=result)
        return json_response(result)

    async def policy_get(self):
        """官方入群自动审批策略。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        force = str(request.query.get("force") or "") in ("1", "true", "yes")
        return json_response(await self.service.policy_snapshot(force=force))

    async def policy_post(self):
        """策略维护（enable/disable/execute/whitelist_add/whitelist_del/create/update/delete）。"""
        if not self._service_ready():
            return error_response("插件尚未初始化完成，请稍后重试")
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not payload.get("op"):
            return error_response("缺少 op")
        result = await self.service.policy_action(payload)
        if isinstance(result, dict) and result.get("ok") is False:
            return error_response(str(result.get("message") or "操作失败"), data=result)
        return json_response(result if isinstance(result, dict) else {"result": result})

    async def instructions(self):
        """指令速查表。"""
        from .commands import (
            CONFIG_COMMANDS,
            INFO_COMMANDS,
            MENU_COMMANDS,
            SELFCHECK_COMMANDS,
            STATUS_COMMANDS,
        )

        return json_response(
            {
                "public": [
                    {"command": MENU_COMMANDS[0], "desc": "显示群管理菜单"},
                    {"command": INFO_COMMANDS[0], "desc": "查看群档案与机器人在群状态"},
                    {"command": STATUS_COMMANDS[0], "desc": "查看本群审核状态"},
                ],
                "group_admin": [
                    {"command": "审核开启 / 审核关闭", "desc": "开关本群内容审核"},
                    {"command": "审核模式 严格/标准/宽松/仅记录", "desc": "切换处置强度"},
                    {"command": "审核阈值 0.0-1.0", "desc": "调整判定置信度门槛"},
                    {"command": "禁言 @某人 [时长] / 解禁 @某人", "desc": "成员禁言管理"},
                    {"command": "撤回（引用消息）", "desc": "撤回 2 分钟内的消息"},
                    {"command": "审核日志 / 审核统计", "desc": "查看记录与统计"},
                    {"command": "入群申请 / 入群通过 <序号> / 入群拒绝 <序号>", "desc": "入群审批"},
                ],
                "admin": [
                    {"command": SELFCHECK_COMMANDS[0], "desc": "平台能力探测"},
                    {"command": CONFIG_COMMANDS[0], "desc": "管理台入口指引"},
                ],
                "note": "指令为全匹配，可带 / 前缀。",
            }
        )
