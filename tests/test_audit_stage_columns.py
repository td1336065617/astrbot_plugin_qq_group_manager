"""0.15.3 审计库新增列（stage/prompt_version/fallback_index）与迁移 fail-fast 测试。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.audit import WRITE_COLUMNS, AuditStore

NEW_COLUMNS = ("stage", "prompt_version", "fallback_index")


def run(coro):
    return asyncio.run(coro)


async def init_store(tmp_path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.db", flush_interval=0.05)
    await store.initialize()
    return store


async def insert_one(store: AuditStore) -> None:
    await store.insert_event(
        group_id="g1",
        verdict="allow",
        category="无",
        severity=1,
        confidence=1.0,
        reason="测试事件",
    )


def columns_of(db_path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(mod_events)")}
    finally:
        conn.close()


def fetch_new_columns(db_path, order: str) -> tuple:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            f"SELECT stage, prompt_version, fallback_index FROM mod_events ORDER BY rowid {order} LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def test_fresh_db_has_new_columns(tmp_path):
    async def scenario():
        store = await init_store(tmp_path)
        await store.close()
    run(scenario())
    assert set(NEW_COLUMNS) <= columns_of(tmp_path / "audit.db")


def test_fresh_db_covers_every_write_column(tmp_path):
    """保护：_open_sync 的启动自检不能因为 WRITE_COLUMNS 里出现 DDL/补列之外的字段而误杀启动。"""

    async def scenario():
        store = await init_store(tmp_path)
        await store.close()
    run(scenario())
    cols = columns_of(tmp_path / "audit.db")
    assert [name for name in WRITE_COLUMNS["events"] if name not in cols] == []


def test_legacy_db_backfilled_and_old_row_gets_default(tmp_path):
    async def create():
        store = await init_store(tmp_path)
        await insert_one(store)
        await store.close()
    run(create())

    db = tmp_path / "audit.db"
    conn = sqlite3.connect(str(db))
    try:
        for name in NEW_COLUMNS:
            conn.execute(f"ALTER TABLE mod_events DROP COLUMN {name}")
    except sqlite3.OperationalError:  # pragma: no cover - 旧 SQLite 不支持 DROP COLUMN
        conn.close()
        pytest.skip("当前 SQLite 不支持 DROP COLUMN，无法模拟老库")
    conn.commit()
    conn.close()

    async def reopen():
        store = await init_store(tmp_path)
        await store.close()
    run(reopen())

    assert set(NEW_COLUMNS) <= columns_of(db)
    assert fetch_new_columns(db, "ASC") == ("judged", "", 0)


def test_insert_event_fills_defaults_when_omitted(tmp_path):
    async def scenario():
        store = await init_store(tmp_path)
        await insert_one(store)
        await store.close()
    run(scenario())
    assert fetch_new_columns(tmp_path / "audit.db", "DESC") == ("judged", "", 0)


def test_open_fails_fast_when_columns_missing(tmp_path, monkeypatch):
    """补列失败时必须启动失败，而不是"动作照做、审计静默丢事件"。"""
    monkeypatch.setattr(AuditStore, "_ensure_columns_sync", staticmethod(lambda *a, **k: None))

    async def scenario():
        store = AuditStore(tmp_path / "audit.db", flush_interval=0.05)
        with pytest.raises(RuntimeError):
            await store.initialize()
    run(scenario())

class _FakeConn:
    """只实现 _ensure_columns_sync 用到的两个行为：PRAGMA 读列 + ALTER。"""

    def __init__(self, existing, alter_error=None):
        self._existing = list(existing)
        self._alter_error = alter_error
        self.alters: list[str] = []

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA table_info"):
            return [(0, name) for name in self._existing]
        if sql.startswith("ALTER TABLE"):
            if self._alter_error is not None:
                raise self._alter_error
            self.alters.append(sql)
            return None
        raise AssertionError(sql)


def test_ensure_columns_tolerates_duplicate_column_name():
    """并发/热重载下后到者必然拿到 duplicate column name：视为成功，不得让插件加载失败。"""
    import sqlite3

    conn = _FakeConn(existing=["id"], alter_error=sqlite3.OperationalError("duplicate column name: stage"))
    AuditStore._ensure_columns_sync(conn, "mod_events", {"stage": "TEXT DEFAULT 'judged'"})


def test_ensure_columns_still_raises_other_errors():
    import sqlite3

    conn = _FakeConn(existing=["id"], alter_error=sqlite3.OperationalError("database is locked"))
    try:
        AuditStore._ensure_columns_sync(conn, "mod_events", {"stage": "TEXT"})
    except sqlite3.OperationalError:
        return
    raise AssertionError("非 duplicate 的错误必须继续抛出")
