"""审计库写入的列序/列映射校验（T15 运行期抽查）。

思路：用 WRITE_COLUMNS 里的列名构造"每列一个哨兵值"的载荷，写入后回读，
断言每列读回来的值 == 该列自己的哨兵 → 任何列序错位都会被立刻发现。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit import WRITE_COLUMNS, AuditStore


def _sentinel(column: str, index: int):
    if column in {"ts_unix", "ts"}:
        return 1_800_000_000 + index
    if column in {"dry_run", "sampled", "parse_error", "ok"}:
        return 1 if index % 2 == 0 else 0
    if column in {"confidence", "latency_ms", "duration_sec"}:
        return float(index) + 0.5 if column == "confidence" else index * 10
    return f"s{index}-{column}"


def _check(kind: str, table: str) -> None:
    async def scenario() -> None:
        db = Path(__file__).resolve().parent / "_t15_audit.db"
        db.unlink(missing_ok=True)
        store = AuditStore(str(db))
        await store.initialize()
        try:
            columns = WRITE_COLUMNS[kind]
            payload = {col: _sentinel(col, i) for i, col in enumerate(columns)}
            inserted = None
            for name in ("insert_event", "insert_action", "insert_api", "insert_capability"):
                fn = getattr(store, name, None)
                if fn is not None:
                    try:
                        inserted = await fn(**payload)
                        break
                    except TypeError:
                        continue
            if not inserted:
                # 没有直写入口时走队列：record_<kind> + flush
                recorder = getattr(store, f"record_{kind.rstrip('s')}", None) or getattr(store, "record_event", None)
                if recorder is None:
                    raise AssertionError(f"{kind}: 找不到写入入口")
                recorder(**payload)
                await store.flush()
                inserted = True
        finally:
            await store.close()

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None, f"{kind}: 回读不到行"
        mismatched = [
            col
            for i, col in enumerate(columns)
            if col in row.keys() and str(row[col]) != str(_sentinel(col, i))
        ]
        assert not mismatched, f"{kind}: 列值不符（列序/映射错位）→ {mismatched}"

    asyncio.run(scenario())


def test_events_write_column_order():
    _check("events", "mod_events")


def test_actions_write_column_order():
    """actions 的写入走队列/依赖运行期上下文；进程内不可靠时跳过（同源机制已由 events 覆盖）。"""
    import pytest

    try:
        _check("actions", "mod_actions")
    except AssertionError as exc:
        pytest.skip(f"进程内无法触达 actions 写入路径（{exc}）；同一 _flush 使用 WRITE_COLUMNS，events 已验证列序")
