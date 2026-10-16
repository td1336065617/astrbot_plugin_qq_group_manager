"""W3：规则本体导出与入群申请过期清理。"""

from __future__ import annotations

import asyncio
import datetime
import sqlite3

from src.audit import AuditStore
from src.rules import export_inventory


def run(coro):
    return asyncio.run(coro)


def test_export_inventory_has_rule_sections():
    inventory = export_inventory()
    assert inventory, "规则本体不应为空"
    assert all(not name.startswith("_") for name in inventory)
    assert any("TEMPLATE" in name for name in inventory), "应包含模板规则组"


def test_expire_pending_joins_marks_only_stale_pending(tmp_path):
    db = tmp_path / "audit.db"

    async def scenario():
        store = AuditStore(db, flush_interval=0.05)
        await store.initialize()
        for join_id, decision in (("old-pending", "pending"), ("new-pending", "pending"), ("old-approved", "approve")):
            await store.record_join(
                join_request_id=join_id,
                group_id="g1",
                member_openid="u1",
                decision=decision,
            )
        await store.close()

        # 直接改 ts_unix：一条 30 天前的 pending，其余保持现在
        conn = sqlite3.connect(db)
        old = (datetime.datetime.now().astimezone() - datetime.timedelta(days=30)).timestamp()
        conn.execute("update join_requests set ts_unix = ? where join_request_id = ?", (int(old), "old-pending"))
        conn.execute("update join_requests set ts_unix = ? where join_request_id = ?", (int(old), "old-approved"))
        conn.commit()
        conn.close()

        store2 = AuditStore(db, flush_interval=0.05)
        await store2.initialize()
        expired = await store2.expire_pending_joins(7)
        await store2.close()
        conn = sqlite3.connect(db)
        rows = dict(conn.execute("select join_request_id, decision from join_requests").fetchall())
        conn.close()
        return expired, rows

    expired, rows = run(scenario())
    assert expired == 1, "只应过期一条"
    assert rows["old-pending"] == "expired"
    assert rows["new-pending"] == "pending"
    assert rows["old-approved"] == "approve"


def test_expire_pending_joins_disabled_when_zero(tmp_path):
    db = tmp_path / "audit.db"

    async def scenario():
        store = AuditStore(db, flush_interval=0.05)
        await store.initialize()
        await store.record_join(join_request_id="j1", group_id="g1", member_openid="u1", decision="pending")
        result = await store.expire_pending_joins(0)
        await store.close()
        return result

    assert run(scenario()) == 0
