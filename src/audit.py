"""SQLite 审计库：审核事件、处置动作、API 调用、能力受限、入群申请、禁言台账。

设计要点（见 docs/设计方案.md §8）：
- 只用标准库 sqlite3（异步通过 asyncio.to_thread），不引入第三方依赖。
- 写入走内存队列 + 批量 flush，绝不在事件循环里做同步 IO。
- 队列满时丢弃并计数，保证业务协程永不被阻塞。
- 单连接 + WAL；写操作由 asyncio.Lock 串行化。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .utils import now_ts, safe_json_dumps, to_iso, truncate

SCHEMA_VERSION = 1

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
      k TEXT PRIMARY KEY,
      v TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mod_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_unix INTEGER NOT NULL,
      ts TEXT NOT NULL,
      group_id TEXT NOT NULL,
      group_name TEXT,
      msg_id TEXT,
      sender_openid TEXT,
      sender_name TEXT,
      sender_role TEXT,
      source TEXT NOT NULL,
      verdict TEXT NOT NULL,
      category TEXT,
      severity INTEGER,
      confidence REAL,
      reason TEXT,
      rule_hits TEXT,
      text_digest TEXT,
      text_excerpt TEXT,
      raw_verdict TEXT,
      latency_ms INTEGER,
      provider_id TEXT,
      dry_run INTEGER NOT NULL DEFAULT 0,
      sampled INTEGER NOT NULL DEFAULT 0,
      parse_error INTEGER NOT NULL DEFAULT 0,
      appealed INTEGER NOT NULL DEFAULT 0,
      appeal_text TEXT,
      appeal_state TEXT,
      appeal_by TEXT,
      appeal_at REAL,
      appeal_note TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON mod_events(ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_group ON mod_events(group_id, ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_verdict ON mod_events(verdict, ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_sender ON mod_events(sender_openid, ts_unix DESC)",
    """
    CREATE TABLE IF NOT EXISTS mod_actions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id INTEGER REFERENCES mod_events(id) ON DELETE CASCADE,
      ts_unix INTEGER NOT NULL,
      group_id TEXT NOT NULL,
      action TEXT NOT NULL,
      target_openid TEXT,
      duration_sec INTEGER,
      until_ts TEXT,
      ok INTEGER NOT NULL,
      dry_run INTEGER NOT NULL DEFAULT 0,
      err_code INTEGER,
      err_msg TEXT,
      trace_id TEXT,
      detail TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_actions_event ON mod_actions(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_actions_ts ON mod_actions(ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_actions_group ON mod_actions(group_id, ts_unix DESC)",
    """
    CREATE TABLE IF NOT EXISTS join_requests (
      join_request_id TEXT PRIMARY KEY,
      ts_unix INTEGER NOT NULL,
      group_id TEXT NOT NULL,
      member_openid TEXT,
      union_openid TEXT,
      username TEXT,
      apply_source TEXT,
      invited_by TEXT,
      is_bot INTEGER,
      risk_tips TEXT,
      verify_method TEXT,
      verify_message TEXT,
      review_qa TEXT,
      decision TEXT,
      decided_by TEXT,
      confidence REAL,
      reason TEXT,
      blacklisted INTEGER NOT NULL DEFAULT 0,
      err_code INTEGER,
      trace_id TEXT,
      profile_source TEXT,
      avatar_url TEXT,
      qq_level INTEGER,
      account_age_days INTEGER,
      reg_time INTEGER,
      qid TEXT,
      profile_json TEXT,
      gate TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_join_group ON join_requests(group_id, ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_join_dec ON join_requests(decision, ts_unix DESC)",
    """
    CREATE TABLE IF NOT EXISTS api_calls (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_unix INTEGER NOT NULL,
      group_id TEXT,
      method TEXT NOT NULL,
      path TEXT NOT NULL,
      ok INTEGER NOT NULL,
      err_code INTEGER,
      trace_id TEXT,
      duration_ms INTEGER,
      retries INTEGER NOT NULL DEFAULT 0,
      caller TEXT,
      dry_run INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_ts ON api_calls(ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_group ON api_calls(group_id, ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_err ON api_calls(ok, err_code, ts_unix DESC)",
    """
    CREATE TABLE IF NOT EXISTS capability_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_unix INTEGER NOT NULL,
      group_id TEXT,
      capability TEXT NOT NULL,
      ok INTEGER NOT NULL,
      err_code INTEGER,
      trace_id TEXT,
      note TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cap_ts ON capability_log(ts_unix DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cap_cap ON capability_log(capability, ok, ts_unix DESC)",
    """
    CREATE TABLE IF NOT EXISTS mutes (
      group_id TEXT NOT NULL,
      member_openid TEXT NOT NULL,
      username TEXT,
      until_unix INTEGER,
      until_ts TEXT,
      reason TEXT,
      event_id INTEGER,
      source TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      updated_unix INTEGER NOT NULL,
      PRIMARY KEY (group_id, member_openid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mutes_until ON mutes(active, until_unix)",
    """
    CREATE TABLE IF NOT EXISTS appeal_whitelist (
      digest     TEXT PRIMARY KEY,
      skeleton   TEXT,
      reason     TEXT,
      added_by   TEXT,
      added_at   REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_daily (
      day TEXT PRIMARY KEY,
      payload TEXT NOT NULL
    )
    """,
)

#: 日志种类 → (表名, 允许筛选的列)
LOG_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "events": (
        "mod_events",
        (
            "group_id",
            "verdict",
            "category",
            "sender_openid",
            "source",
            "dry_run",
            "appealed",
            "appeal_state",
        ),
    ),
    "actions": (
        "mod_actions",
        ("group_id", "action", "ok", "target_openid", "dry_run", "event_id"),
    ),
    "api": (
        "api_calls",
        ("group_id", "method", "ok", "err_code", "caller", "dry_run"),
    ),
    "capability": ("capability_log", ("group_id", "capability", "ok", "err_code")),
}

#: 日志种类 → 写入用的白名单列
WRITE_COLUMNS: dict[str, tuple[str, ...]] = {
    "events": (
        "ts_unix",
        "ts",
        "group_id",
        "group_name",
        "msg_id",
        "sender_openid",
        "sender_name",
        "sender_role",
        "source",
        "verdict",
        "category",
        "severity",
        "confidence",
        "reason",
        "rule_hits",
        "text_digest",
        "text_excerpt",
        "raw_verdict",
        "latency_ms",
        "provider_id",
        "dry_run",
        "sampled",
        "parse_error",
        "appeal_by",
        "appeal_at",
        "appeal_note",
    ),
    "actions": (
        "ts_unix",
        "event_id",
        "group_id",
        "action",
        "target_openid",
        "duration_sec",
        "until_ts",
        "ok",
        "dry_run",
        "err_code",
        "err_msg",
        "trace_id",
        "detail",
    ),
    "api": (
        "ts_unix",
        "group_id",
        "method",
        "path",
        "ok",
        "err_code",
        "trace_id",
        "duration_ms",
        "retries",
        "caller",
        "dry_run",
    ),
    "capability": (
        "ts_unix",
        "group_id",
        "capability",
        "ok",
        "err_code",
        "trace_id",
        "note",
    ),
}

#: 老库补列（新库由 DDL 建全）；值是对应的 SQLite 列类型
EVENT_EXTRA_COLUMNS: dict[str, str] = {
    "appeal_by": "TEXT",
    "appeal_at": "REAL",
    "appeal_note": "TEXT",
}

#: 入群申请画像相关补列（申请人画像增强，0.12.0）
JOIN_EXTRA_COLUMNS: dict[str, str] = {
    "profile_source": "TEXT",
    "avatar_url": "TEXT",
    "qq_level": "INTEGER",
    "account_age_days": "INTEGER",
    "reg_time": "INTEGER",
    "qid": "TEXT",
    "profile_json": "TEXT",
    "gate": "TEXT",
}

#: NOT NULL 列的兜底默认值（未提供时避免 IntegrityError）
COLUMN_DEFAULTS: dict[str, Any] = {
    "ok": 0,
    "dry_run": 0,
    "sampled": 0,
    "parse_error": 0,
    "appealed": 0,
    "retries": 0,
    "blacklisted": 0,
    "active": 1,
    "method": "",
    "path": "",
    "source": "llm",
    "verdict": "review",
    "action": "",
    "capability": "",
}

#: 带时间戳的表（用于保留期裁剪）
RETENTION_TABLES: dict[str, str] = {
    "events": "mod_events",
    "actions": "mod_actions",
    "api": "api_calls",
    "capability": "capability_log",
    "join": "join_requests",
}


class AuditStore:
    """SQLite 审计库（异步封装 + 批量写）。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        queue_maxsize: int = 5000,
        batch_size: int = 50,
        flush_interval: float = 1.0,
        logger: Any = None,
        on_record: Any = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.on_record = on_record
        """可选回调 on_record(kind, payload)：用于把新日志实时推给 WebUI（SSE）。"""
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=max(10, int(queue_maxsize))
        )
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.1, float(flush_interval))
        self.logger = logger
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False
        self.stats: dict[str, Any] = {
            "queued": 0,
            "written": 0,
            "dropped": 0,
            "errors": 0,
            "last_error": "",
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """建库、建表、启动写线程协程。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._open_sync)
        self._writer = asyncio.create_task(self._writer_loop(), name="qqgm-audit-writer")

    def _open_sync(self) -> None:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in DDL_STATEMENTS:
            conn.execute(statement)
        # 老库补列（幂等）：失败只记 warning，不阻塞启动
        try:
            self._ensure_columns_sync(conn, "mod_events", EVENT_EXTRA_COLUMNS)
            self._ensure_columns_sync(conn, "join_requests", JOIN_EXTRA_COLUMNS)
        except Exception as exc:  # pragma: no cover - 迁移失败不应阻塞启动
            if self.logger is not None:
                self.logger.warning("审计库补列失败（申诉/画像功能可能不可用）：%s", exc)
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        self._conn = conn

    async def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        """幂等补列（异步包装，便于测试/外部调用）。"""
        await asyncio.to_thread(self._ensure_columns_sync, conn, table, columns)

    @staticmethod
    def _ensure_columns_sync(
        conn: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        """幂等补列：PRAGMA 查现有列，缺哪个补哪个（SQLite 支持 ADD COLUMN）。"""
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    async def close(self, timeout: float = 3.0) -> None:
        """停止写协程、落盘剩余记录、关闭连接。

        先唤醒写协程并等它自然退出，再关闭连接：sqlite3 连接不支持跨线程并发使用，
        直接 cancel 会让 `_write` 的线程在后台继续跑，与 close 的 commit/close 竞争
        （表现为 `cannot commit - no transaction is active`，极端情况会段错误）。
        """
        self._closed = True
        if self._writer is not None:
            try:  # 唤醒阻塞在 queue.get 上的写协程
                self.queue.put_nowait(("__stop__", {}))
            except asyncio.QueueFull:  # pragma: no cover - 队列满时靠超时退出
                pass
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._writer, return_exceptions=True), timeout
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._writer.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(self._writer, return_exceptions=True), timeout
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            self._writer = None
        try:
            await asyncio.wait_for(self.flush(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        if self._conn is not None:
            conn = self._conn
            self._conn = None

            def _close() -> None:
                try:
                    conn.commit()
                finally:
                    conn.close()

            await asyncio.to_thread(_close)

    async def _writer_loop(self) -> None:
        """周期性把队列里的记录批量写库。"""
        while not self._closed:
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                await self.flush()
                continue
            except asyncio.CancelledError:
                break
            items = [first]
            deadline = time.monotonic() + self.flush_interval
            while len(items) < self.batch_size and time.monotonic() < deadline:
                try:
                    items.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._write(items)

    async def flush(self) -> None:
        """立即写入队列中已有的记录。"""
        items: list[tuple[str, dict[str, Any]]] = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(items) >= self.batch_size * 4:
                break
        if items:
            await self._write(items)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict[str, Any]) -> bool:
        """把一条记录放入写队列（非阻塞；队列满则丢弃并计数）。"""
        if self._closed:
            return False
        try:
            self.queue.put_nowait((kind, payload))
            self.stats["queued"] += 1
            if self.on_record is not None:
                try:
                    self.on_record(kind, payload)
                except Exception:  # pragma: no cover - 推送失败不影响写入
                    pass
            return True
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            if self.logger is not None and self.stats["dropped"] % 100 == 1:
                self.logger.warning("审计写队列已满，累计丢弃 %d 条", self.stats["dropped"])
            return False

    async def _write(self, items: list[tuple[str, dict[str, Any]]]) -> None:
        if not items or self._conn is None:
            return
        grouped: dict[str, list[tuple[Any, ...]]] = {}
        for kind, payload in items:
            columns = WRITE_COLUMNS.get(kind)
            if not columns:
                continue
            record = dict(payload)
            record.setdefault("ts_unix", now_ts())
            row = tuple(self._coerce(record.get(column), column=column) for column in columns)
            grouped.setdefault(kind, []).append(row)

        async with self._lock:
            conn = self._conn
            if conn is None:
                return

            def _run() -> int:
                written = 0
                for kind, rows in grouped.items():
                    columns = WRITE_COLUMNS[kind]
                    table = LOG_TABLES[kind][0]
                    placeholders = ", ".join("?" for _ in columns)
                    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
                    conn.executemany(sql, rows)
                    written += len(rows)
                conn.commit()
                return written

            try:
                self.stats["written"] += await asyncio.to_thread(_run)
                self.stats["last_error"] = ""
            except Exception as exc:  # pragma: no cover - 审计失败不应影响业务
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                try:  # 回滚失败的批次，避免留下悬挂事务导致后续操作阻塞
                    await asyncio.to_thread(conn.rollback)
                except Exception:
                    pass
                if self.logger is not None:
                    self.logger.error("写审计库失败：%s", exc)

    @staticmethod
    def _coerce(value: Any, *, column: str = "") -> Any:
        """把 Python 值转换为 sqlite3 可接受的类型（NOT NULL 列补默认值）。"""
        if value is None:
            return COLUMN_DEFAULTS.get(column)
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float, str, bytes)):
            return value
        return safe_json_dumps(value)

    # -- 业务便捷写入 --------------------------------------------------
    def record_api_call(
        self,
        *,
        group_id: str | None,
        method: str,
        path: str,
        ok: bool,
        err_code: int | None = None,
        trace_id: str = "",
        duration_ms: int = 0,
        retries: int = 0,
        caller: str = "",
        dry_run: bool = False,
    ) -> bool:
        return self.enqueue(
            "api",
            {
                "group_id": group_id,
                "method": method,
                "path": path,
                "ok": ok,
                "err_code": err_code,
                "trace_id": trace_id,
                "duration_ms": duration_ms,
                "retries": retries,
                "caller": caller,
                "dry_run": dry_run,
            },
        )

    def record_capability(
        self,
        *,
        group_id: str | None,
        capability: str,
        ok: bool,
        err_code: int | None = None,
        trace_id: str = "",
        note: str = "",
    ) -> bool:
        return self.enqueue(
            "capability",
            {
                "group_id": group_id,
                "capability": capability,
                "ok": ok,
                "err_code": err_code,
                "trace_id": trace_id,
                "note": note,
            },
        )

    async def insert_event(self, **payload: Any) -> int | None:
        """同步写入一条审核事件并返回自增 id（动作表需要它做外键）。

        事件写入频率与 LLM 调用同量级，直接同步写可接受；API 调用/能力日志
        这类高频记录仍走队列批量写。
        """
        payload.setdefault("ts_unix", now_ts())
        payload.setdefault("ts", to_iso(payload["ts_unix"]))
        columns = WRITE_COLUMNS["events"]
        values = tuple(self._coerce(payload.get(column), column=column) for column in columns)
        conn = self._conn
        if conn is None:
            return None
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO mod_events ({', '.join(columns)}) VALUES ({placeholders})"
        async with self._lock:
            try:
                cursor = await asyncio.to_thread(conn.execute, sql, values)
                await asyncio.to_thread(conn.commit)
                return int(cursor.lastrowid or 0)
            except Exception as exc:  # pragma: no cover - 审计失败不应影响处置
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    await asyncio.to_thread(conn.rollback)
                except Exception:
                    pass
                if self.logger is not None:
                    self.logger.error("写入审核事件失败：%s", exc)
                return None

    async def find_last_event(self, group_id: str, member_openid: str) -> dict[str, Any] | None:
        """取该成员在本群最近一条被处置（非 allow）的记录，用于申诉关联。"""
        rows = await self._fetch_all(
            "SELECT * FROM mod_events WHERE group_id = ? AND sender_openid = ? "
            "AND verdict != 'allow' ORDER BY ts_unix DESC LIMIT 1",
            (group_id, member_openid),
        )
        return rows[0] if rows else None

    async def find_event_by_msg_id(self, group_id: str, msg_id: str) -> dict[str, Any] | None:
        """按被引用消息 ID 找审核事件（申诉优先用 Reply 关联）。"""
        if not msg_id:
            return None
        rows = await self._fetch_all(
            "SELECT * FROM mod_events WHERE group_id = ? AND msg_id = ? "
            "ORDER BY ts_unix DESC LIMIT 1",
            (group_id, msg_id),
        )
        return rows[0] if rows else None

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        """按事件 ID 取审核事件（管理台处理申诉用）。"""
        if not event_id:
            return None
        rows = await self._fetch_all(
            "SELECT * FROM mod_events WHERE id = ? LIMIT 1", (int(event_id),)
        )
        return rows[0] if rows else None

    async def event_has_action(self, event_id: int, action: str = "mute") -> bool:
        """该事件是否成功执行过某动作（申诉通过时判断要不要解禁）。"""
        if not event_id:
            return False
        rows = await self._fetch_all(
            "SELECT id FROM mod_actions WHERE event_id = ? AND action = ? AND ok = 1 LIMIT 1",
            (int(event_id), str(action)),
        )
        return bool(rows)

    async def create_appeal(self, event_id: int, text: str) -> bool:
        """写 appealed=1 / appeal_text / appeal_state='pending'（不覆盖历史处理备注）。"""
        conn = self._conn
        if conn is None or not event_id:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(
                    conn.execute,
                    "UPDATE mod_events SET appealed = 1, appeal_text = ?, "
                    "appeal_state = 'pending' WHERE id = ?",
                    (truncate(text, 200), int(event_id)),
                )
                await asyncio.to_thread(conn.commit)
                return True
            except Exception as exc:  # pragma: no cover
                if self.logger is not None:
                    self.logger.error("写入申诉失败：%s", exc)
                return False

    async def mark_appeal(self, event_id: int, text: str, *, state: str = "pending") -> bool:
        """兼容旧调用：pending 走 create_appeal，其余状态只改状态（保留申诉正文）。"""
        if state == "pending":
            return await self.create_appeal(event_id, text)
        conn = self._conn
        if conn is None or not event_id:
            return False
        async with self._lock:
            try:
                if text:
                    await asyncio.to_thread(
                        conn.execute,
                        "UPDATE mod_events SET appealed = 1, appeal_state = ?, appeal_text = ? "
                        "WHERE id = ?",
                        (state, truncate(text, 200), int(event_id)),
                    )
                else:
                    await asyncio.to_thread(
                        conn.execute,
                        "UPDATE mod_events SET appealed = 1, appeal_state = ? WHERE id = ?",
                        (state, int(event_id)),
                    )
                await asyncio.to_thread(conn.commit)
                return True
            except Exception as exc:  # pragma: no cover
                if self.logger is not None:
                    self.logger.error("写入申诉标记失败：%s", exc)
                return False

    async def resolve_appeal(
        self,
        event_id: int,
        *,
        accepted: bool,
        by: str = "system",
        note: str = "",
    ) -> bool:
        """写申诉结果：appeal_state + appeal_by + appeal_at + appeal_note；不覆盖 appeal_text。"""
        conn = self._conn
        if conn is None or not event_id:
            return False
        state = "accepted" if accepted else "rejected"
        async with self._lock:
            try:
                await asyncio.to_thread(
                    conn.execute,
                    "UPDATE mod_events SET appeal_state = ?, appeal_by = ?, appeal_at = ?, "
                    "appeal_note = ? WHERE id = ?",
                    (
                        state,
                        str(by or "system"),
                        float(now_ts()),
                        truncate(note, 200),
                        int(event_id),
                    ),
                )
                await asyncio.to_thread(conn.commit)
                return True
            except Exception as exc:  # pragma: no cover
                if self.logger is not None:
                    self.logger.error("写入申诉结果失败：%s", exc)
                return False

    async def list_appeals(
        self,
        *,
        state: str = "pending",
        group_id: str = "",
        days: int = 30,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出申诉记录（state 传空或 all 表示不筛选状态）。"""
        sql = "SELECT * FROM mod_events WHERE appealed = 1"
        params: list[Any] = []
        if state and state != "all":
            sql += " AND appeal_state = ?"
            params.append(str(state))
        if group_id:
            sql += " AND group_id = ?"
            params.append(str(group_id))
        try:
            window = int(days)
        except (TypeError, ValueError):
            window = 30
        if window > 0:
            sql += " AND ts_unix >= ?"
            params.append(now_ts() - window * 86400)
        sql += " ORDER BY ts_unix DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        return await self._fetch_all(sql, tuple(params))

    # -- 误判自学习白名单 ----------------------------------------------
    async def whitelist_contains(self, digest: str) -> bool:
        """该摘要是否在申诉白名单里（命中则跳过 LLM 送审，但仍写审计）。"""
        if not digest:
            return False
        rows = await self._fetch_all(
            "SELECT digest FROM appeal_whitelist WHERE digest = ? LIMIT 1", (str(digest),)
        )
        return bool(rows)

    async def whitelist_add(self, digest: str, skeleton: str, reason: str, by: str) -> None:
        """写入/更新申诉白名单条目。"""
        conn = self._conn
        if conn is None or not digest:
            return
        async with self._lock:
            try:
                await asyncio.to_thread(
                    conn.execute,
                    "INSERT INTO appeal_whitelist(digest, skeleton, reason, added_by, added_at) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(digest) DO UPDATE SET "
                    "skeleton=excluded.skeleton, reason=excluded.reason, "
                    "added_by=excluded.added_by, added_at=excluded.added_at",
                    (
                        str(digest),
                        truncate(skeleton, 200),
                        truncate(reason, 200),
                        str(by or "system"),
                        float(now_ts()),
                    ),
                )
                await asyncio.to_thread(conn.commit)
            except Exception as exc:  # pragma: no cover
                if self.logger is not None:
                    self.logger.error("写入申诉白名单失败：%s", exc)

    async def whitelist_remove(self, digest: str) -> bool:
        """撤销白名单条目，返回是否真的删掉了一行。"""
        conn = self._conn
        if conn is None or not digest:
            return False
        async with self._lock:
            try:
                cursor = await asyncio.to_thread(
                    conn.execute,
                    "DELETE FROM appeal_whitelist WHERE digest = ?",
                    (str(digest),),
                )
                await asyncio.to_thread(conn.commit)
                return bool(cursor.rowcount)
            except Exception as exc:  # pragma: no cover
                if self.logger is not None:
                    self.logger.error("撤销申诉白名单失败：%s", exc)
                return False

    async def whitelist_list(self, limit: int = 200) -> list[dict[str, Any]]:
        """列出申诉白名单条目（管理台展示与撤销）。"""
        return await self._fetch_all(
            "SELECT * FROM appeal_whitelist ORDER BY added_at DESC LIMIT ?",
            (max(1, min(1000, int(limit))),),
        )

    # -- 周报聚合（B2 使用） -------------------------------------------
    @staticmethod
    def _summary_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "total": int(row.get("total") or 0),
            "violation": int(row.get("violation") or 0),
            "review": int(row.get("review") or 0),
            "allow": int(row.get("allow_count") or 0),
            "appeals": int(row.get("appeals") or 0),
            "accepted": int(row.get("accepted") or 0),
        }

    async def summary_by_category(self, days: int = 7) -> list[dict[str, Any]]:
        """近 N 天按分类聚合（审核量/违规量/申诉量与通过量）。"""
        since = now_ts() - max(1, int(days)) * 86400
        rows = await self._fetch_all(
            "SELECT COALESCE(category, '') AS category, COUNT(*) AS total, "
            "SUM(CASE WHEN verdict = 'violation' THEN 1 ELSE 0 END) AS violation, "
            "SUM(CASE WHEN verdict = 'review' THEN 1 ELSE 0 END) AS review, "
            "SUM(CASE WHEN verdict = 'allow' THEN 1 ELSE 0 END) AS allow_count, "
            "SUM(CASE WHEN appealed = 1 THEN 1 ELSE 0 END) AS appeals, "
            "SUM(CASE WHEN appeal_state = 'accepted' THEN 1 ELSE 0 END) AS accepted "
            "FROM mod_events WHERE ts_unix >= ? GROUP BY category ORDER BY total DESC",
            (since,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._summary_row(row)
            item["category"] = str(row.get("category") or "")
            result.append(item)
        return result

    async def summary_by_group(self, days: int = 7) -> list[dict[str, Any]]:
        """近 N 天按群聚合（审核量/违规量/申诉量与通过量）。"""
        since = now_ts() - max(1, int(days)) * 86400
        rows = await self._fetch_all(
            "SELECT group_id, COALESCE(group_name, '') AS group_name, COUNT(*) AS total, "
            "SUM(CASE WHEN verdict = 'violation' THEN 1 ELSE 0 END) AS violation, "
            "SUM(CASE WHEN verdict = 'review' THEN 1 ELSE 0 END) AS review, "
            "SUM(CASE WHEN verdict = 'allow' THEN 1 ELSE 0 END) AS allow_count, "
            "SUM(CASE WHEN appealed = 1 THEN 1 ELSE 0 END) AS appeals, "
            "SUM(CASE WHEN appeal_state = 'accepted' THEN 1 ELSE 0 END) AS accepted "
            "FROM mod_events WHERE ts_unix >= ? "
            "GROUP BY group_id, group_name ORDER BY total DESC",
            (since,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._summary_row(row)
            item["group_id"] = str(row.get("group_id") or "")
            item["group_name"] = str(row.get("group_name") or "")
            result.append(item)
        return result

    def record_event(self, **payload: Any) -> bool:
        payload.setdefault("ts_unix", now_ts())
        payload.setdefault("ts", to_iso(payload["ts_unix"]))
        return self.enqueue("events", payload)

    def record_action(self, **payload: Any) -> bool:
        payload.setdefault("ts_unix", now_ts())
        return self.enqueue("actions", payload)

    async def record_join(self, **payload: Any) -> None:
        """入群申请带主键，需要 UPSERT，因此直接同步写（频率低）。"""
        payload.setdefault("ts_unix", now_ts())
        columns = (
            "join_request_id",
            "ts_unix",
            "group_id",
            "member_openid",
            "union_openid",
            "username",
            "apply_source",
            "invited_by",
            "is_bot",
            "risk_tips",
            "verify_method",
            "verify_message",
            "review_qa",
            "decision",
            "decided_by",
            "confidence",
            "reason",
            "blacklisted",
            "err_code",
            "trace_id",
            "profile_source",
            "avatar_url",
            "qq_level",
            "account_age_days",
            "reg_time",
            "qid",
            "profile_json",
            "gate",
        )
        values = tuple(self._coerce(payload.get(column), column=column) for column in columns)
        async with self._lock:
            conn = self._conn
            if conn is None:
                return
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(
                f"{column}=excluded.{column}" for column in columns if column != "join_request_id"
            )
            sql = (
                f"INSERT INTO join_requests ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(join_request_id) DO UPDATE SET {updates}"
            )
            await asyncio.to_thread(conn.execute, sql, values)
            await asyncio.to_thread(conn.commit)

    async def get_join(self, join_request_id: str) -> dict[str, Any] | None:
        """按申请 ID 查询入群申请记录（用于幂等去重）。"""
        rows = await self._fetch_all(
            "SELECT * FROM join_requests WHERE join_request_id = ?", (join_request_id,)
        )
        return rows[0] if rows else None

    async def list_joins(
        self, group_id: str | None = None, *, decision: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """列出入群申请记录。"""
        sql = "SELECT * FROM join_requests WHERE 1=1"
        params: list[Any] = []
        if group_id:
            sql += " AND group_id = ?"
            params.append(group_id)
        if decision:
            sql += " AND decision = ?"
            params.append(decision)
        sql += " ORDER BY ts_unix DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        return await self._fetch_all(sql, tuple(params))

    async def upsert_mute(
        self,
        *,
        group_id: str,
        member_openid: str,
        username: str = "",
        until_unix: int | None = None,
        reason: str = "",
        event_id: int | None = None,
        source: str = "auto",
        active: bool = True,
    ) -> None:
        """写入/更新本地禁言台账。"""
        row = (
            group_id,
            member_openid,
            username,
            until_unix,
            to_iso(until_unix) if until_unix else "",
            reason,
            event_id,
            source,
            1 if active else 0,
            now_ts(),
        )
        async with self._lock:
            conn = self._conn
            if conn is None:
                return
            sql = (
                "INSERT INTO mutes (group_id, member_openid, username, until_unix, "
                "until_ts, reason, event_id, source, active, updated_unix) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, member_openid) DO UPDATE SET "
                "username=excluded.username, until_unix=excluded.until_unix, "
                "until_ts=excluded.until_ts, reason=excluded.reason, "
                "event_id=excluded.event_id, source=excluded.source, "
                "active=excluded.active, updated_unix=excluded.updated_unix"
            )
            await asyncio.to_thread(conn.execute, sql, row)
            await asyncio.to_thread(conn.commit)

    async def list_mutes(
        self, group_id: str | None = None, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """查询本地禁言台账。"""
        sql = "SELECT * FROM mutes WHERE 1=1"
        params: list[Any] = []
        if group_id:
            sql += " AND group_id = ?"
            params.append(group_id)
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY until_unix ASC"
        return await self._fetch_all(sql, tuple(params))

    async def set_mute_active(self, group_id: str, member_openid: str, active: bool) -> None:
        """把台账中的某条禁言标记为失效（解禁后调用）。"""
        async with self._lock:
            conn = self._conn
            if conn is None:
                return
            sql = (
                "UPDATE mutes SET active = ?, updated_unix = ? "
                "WHERE group_id = ? AND member_openid = ?"
            )
            await asyncio.to_thread(
                conn.execute, sql, (1 if active else 0, now_ts(), group_id, member_openid)
            )
            await asyncio.to_thread(conn.commit)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        conn = self._conn
        if conn is None:
            return []

        def _run() -> list[dict[str, Any]]:
            cursor = conn.execute(sql, params)
            columns = [item[0] for item in cursor.description or []]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # pragma: no cover
            if self.logger is not None:
                self.logger.error("查询审计库失败：%s", exc)
            return []

    @staticmethod
    def _build_filters(kind: str, filters: dict[str, Any] | None) -> tuple[list[str], list[Any]]:
        """把筛选条件翻译为 SQL 片段（列名走白名单，避免注入）。"""
        clauses: list[str] = []
        params: list[Any] = []
        _table, allowed = LOG_TABLES[kind]
        data = filters or {}
        for key, value in data.items():
            if value in (None, "", []):
                continue
            if key in allowed:
                if isinstance(value, (list, tuple, set)):
                    values = [item for item in value if item not in (None, "")]
                    if not values:
                        continue
                    clauses.append(f"{key} IN ({', '.join('?' for _ in values)})")
                    params.extend(values)
                else:
                    clauses.append(f"{key} = ?")
                    params.append(value)
            elif key == "ts_from":
                clauses.append("ts_unix >= ?")
                params.append(int(value))
            elif key == "ts_to":
                clauses.append("ts_unix <= ?")
                params.append(int(value))
            elif key == "keyword" and kind == "events":
                clauses.append("(text_excerpt LIKE ? OR sender_name LIKE ? OR msg_id LIKE ?)")
                like = f"%{value}%"
                params.extend([like, like, like])
            elif key == "keyword" and kind == "api":
                clauses.append("(path LIKE ? OR caller LIKE ?)")
                like = f"%{value}%"
                params.extend([like, like])
        return clauses, params

    async def query_logs(
        self,
        kind: str,
        *,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        order: str = "ts_unix DESC",
    ) -> dict[str, Any]:
        """分页查询日志（返回 {items, total, page, page_size}）。"""
        if kind not in LOG_TABLES:
            raise ValueError(f"未知日志类型：{kind}")
        table = LOG_TABLES[kind][0]
        clauses, params = self._build_filters(kind, filters)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        offset = (page - 1) * page_size
        safe_order = (
            order
            if order.split()[0]
            in {
                "ts_unix",
                "id",
                "severity",
                "confidence",
                "until_unix",
            }
            else "ts_unix DESC"
        )
        direction = "ASC" if safe_order.upper().endswith("ASC") else "DESC"
        column = safe_order.split()[0]
        items = await self._fetch_all(
            f"SELECT * FROM {table}{where} ORDER BY {column} {direction} LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        total_rows = await self._fetch_all(
            f"SELECT COUNT(*) AS total FROM {table}{where}", tuple(params)
        )
        total = int(total_rows[0]["total"]) if total_rows else 0
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def summary(self, days: int = 1) -> dict[str, Any]:
        """近 N 天概览统计（供 WebUI 总览使用）。"""
        since = now_ts() - max(1, int(days)) * 86400
        events = await self._fetch_all(
            "SELECT verdict, COUNT(*) AS n FROM mod_events WHERE ts_unix >= ? GROUP BY verdict",
            (since,),
        )
        actions = await self._fetch_all(
            "SELECT action, ok, COUNT(*) AS n FROM mod_actions WHERE ts_unix >= ? "
            "GROUP BY action, ok",
            (since,),
        )
        api_errors = await self._fetch_all(
            "SELECT err_code, COUNT(*) AS n FROM api_calls WHERE ts_unix >= ? AND ok = 0 "
            "GROUP BY err_code ORDER BY n DESC LIMIT 10",
            (since,),
        )
        capability = await self._fetch_all(
            "SELECT capability, err_code, COUNT(*) AS n FROM capability_log "
            "WHERE ts_unix >= ? AND ok = 0 GROUP BY capability, err_code "
            "ORDER BY n DESC LIMIT 10",
            (since,),
        )
        by_verdict = {str(row["verdict"]): int(row["n"]) for row in events}
        by_action: dict[str, dict[str, int]] = {}
        for row in actions:
            bucket = by_action.setdefault(str(row["action"]), {"ok": 0, "fail": 0})
            key = "ok" if int(row["ok"]) else "fail"
            bucket[key] += int(row["n"])
        return {
            "days": days,
            "since": to_iso(since),
            "verdicts": by_verdict,
            "events_total": sum(by_verdict.values()),
            "actions": by_action,
            "api_errors": [
                {"err_code": row["err_code"], "count": int(row["n"])} for row in api_errors
            ],
            "capability_denied": [
                {
                    "capability": row["capability"],
                    "err_code": row["err_code"],
                    "count": int(row["n"]),
                }
                for row in capability
            ],
            "queue": dict(self.stats),
        }

    async def db_info(self) -> dict[str, Any]:
        """数据库体积、各表行数与时间范围。"""
        info: dict[str, Any] = {
            "path": str(self.db_path),
            "size_bytes": 0,
            "wal_bytes": 0,
            "tables": {},
        }
        try:
            info["size_bytes"] = self.db_path.stat().st_size if self.db_path.exists() else 0
            wal = Path(str(self.db_path) + "-wal")
            info["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        except OSError:  # pragma: no cover
            pass
        for kind, table in RETENTION_TABLES.items():
            rows = await self._fetch_all(
                f"SELECT COUNT(*) AS n, MIN(ts_unix) AS min_ts, MAX(ts_unix) AS max_ts FROM {table}",
                (),
            )
            row = rows[0] if rows else {"n": 0, "min_ts": None, "max_ts": None}
            info["tables"][kind] = {
                "table": table,
                "count": int(row.get("n") or 0),
                "oldest": to_iso(row["min_ts"]) if row.get("min_ts") else None,
                "newest": to_iso(row["max_ts"]) if row.get("max_ts") else None,
            }
        mutes = await self._fetch_all("SELECT COUNT(*) AS n FROM mutes WHERE active = 1", ())
        info["tables"]["mutes"] = {"table": "mutes", "count": int(mutes[0]["n"]) if mutes else 0}
        return info

    async def prune(self, retention: dict[str, int]) -> dict[str, int]:
        """按保留天数裁剪历史数据，返回各表删除行数。"""
        deleted: dict[str, int] = {}
        for kind, days in retention.items():
            table = RETENTION_TABLES.get(kind)
            if not table:
                continue
            cutoff = now_ts() - max(1, int(days)) * 86400
            conn = self._conn
            if conn is None:
                break
            async with self._lock:
                cursor = await asyncio.to_thread(
                    conn.execute, f"DELETE FROM {table} WHERE ts_unix < ?", (cutoff,)
                )
                await asyncio.to_thread(conn.commit)
                deleted[kind] = int(cursor.rowcount or 0)
        # 清理已过期的禁言台账
        conn = self._conn
        if conn is not None:
            async with self._lock:
                cursor = await asyncio.to_thread(
                    conn.execute,
                    "UPDATE mutes SET active = 0 WHERE active = 1 AND until_unix IS NOT NULL "
                    "AND until_unix < ?",
                    (now_ts(),),
                )
                await asyncio.to_thread(conn.commit)
                deleted["mutes_expired"] = int(cursor.rowcount or 0)
        return deleted

    async def vacuum(self) -> None:
        """整理数据库文件（WAL 检查点 + VACUUM）。"""
        conn = self._conn
        if conn is None:
            return
        async with self._lock:
            await asyncio.to_thread(conn.execute, "PRAGMA wal_checkpoint(TRUNCATE)")
            await asyncio.to_thread(conn.execute, "VACUUM")
            await asyncio.to_thread(conn.commit)

    async def clear(self, scope: str, *, before_days: int | None = None) -> dict[str, int]:
        """清空指定范围的日志（scope: events/actions/api/capability/join/all）。"""
        kinds = list(RETENTION_TABLES) if scope == "all" else [scope]
        deleted: dict[str, int] = {}
        conn = self._conn
        if conn is None:
            return deleted
        for kind in kinds:
            table = RETENTION_TABLES.get(kind)
            if not table:
                continue
            sql = f"DELETE FROM {table}"
            params: tuple[Any, ...] = ()
            if before_days is not None:
                sql += " WHERE ts_unix < ?"
                params = (now_ts() - max(0, int(before_days)) * 86400,)
            async with self._lock:
                cursor = await asyncio.to_thread(conn.execute, sql, params)
                await asyncio.to_thread(conn.commit)
                deleted[kind] = int(cursor.rowcount or 0)
        return deleted

    async def backup(self, dest: str | Path) -> Path:
        """把数据库一致性备份到 dest（使用 sqlite3 的 backup API）。"""
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn
        if conn is None:
            raise RuntimeError("审计库未初始化")
        async with self._lock:

            def _run() -> None:
                with sqlite3.connect(str(target)) as dest_conn:
                    conn.backup(dest_conn)

            await asyncio.to_thread(_run)
        return target

    async def save_daily_stats(self, day: str, payload: dict[str, Any]) -> None:
        """写入/覆盖某天的统计快照。"""
        conn = self._conn
        if conn is None:
            return
        async with self._lock:
            await asyncio.to_thread(
                conn.execute,
                "INSERT INTO stats_daily(day, payload) VALUES(?, ?) "
                "ON CONFLICT(day) DO UPDATE SET payload=excluded.payload",
                (day, json.dumps(payload, ensure_ascii=False)),
            )
            await asyncio.to_thread(conn.commit)

    async def get_daily_stats(self, days: int = 30) -> list[dict[str, Any]]:
        """读取最近的日统计快照。"""
        rows = await self._fetch_all(
            "SELECT day, payload FROM stats_daily ORDER BY day DESC LIMIT ?",
            (max(1, int(days)),),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {}
            result.append({"day": row["day"], "payload": payload})
        return result
