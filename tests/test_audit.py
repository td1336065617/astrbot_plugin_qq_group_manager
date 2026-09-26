"""AuditStore 单元测试：建表、批量写、查询、裁剪、禁言台账。"""

from __future__ import annotations

import asyncio

from src.audit import AuditStore


def run(coro):
    return asyncio.run(coro)


async def make_store(tmp_path, **kwargs) -> AuditStore:
    store = AuditStore(tmp_path / "audit.db", flush_interval=0.05, **kwargs)
    await store.initialize()
    return store


def test_record_and_query_api_calls(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        store.record_api_call(
            group_id="g1",
            method="GET",
            path="/v2/groups/{group_openid}/info",
            ok=True,
            err_code=0,
            caller="probe",
        )
        store.record_api_call(
            group_id="g2",
            method="POST",
            path="/v2/groups/{group_openid}/restrict_chat_setting",
            ok=False,
            err_code=11253,
            caller="moderation",
        )
        await store.flush()
        result = await store.query_logs("api", page=1, page_size=10)
        assert result["total"] == 2
        filtered = await store.query_logs("api", filters={"group_id": "g2"})
        assert filtered["total"] == 1
        assert filtered["items"][0]["err_code"] == 11253
        failed = await store.query_logs("api", filters={"ok": 0})
        assert failed["total"] == 1
        await store.close()

    run(scenario())


def test_events_actions_and_summary(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        store.record_event(
            group_id="g1",
            source="llm",
            verdict="violation",
            category="广告引流",
            severity=3,
            confidence=0.9,
            reason="含引流链接",
            sender_openid="u1",
            sender_name="某人",
            text_excerpt="加群 xxx",
            dry_run=True,
        )
        store.record_event(group_id="g1", source="llm", verdict="allow", severity=1)
        await store.flush()
        events = await store.query_logs("events", filters={"verdict": "violation"})
        assert events["total"] == 1
        event_id = events["items"][0]["id"]
        store.record_action(event_id=event_id, group_id="g1", action="warn", ok=True, dry_run=True)
        await store.flush()
        actions = await store.query_logs("actions", filters={"action": "warn"})
        assert actions["total"] == 1
        summary = await store.summary(1)
        assert summary["events_total"] == 2
        assert summary["verdicts"]["violation"] == 1
        assert summary["actions"]["warn"]["ok"] == 1
        await store.close()

    run(scenario())


def test_capability_log_and_db_info(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        store.record_capability(
            group_id="g1",
            capability="member_list",
            ok=False,
            err_code=11253,
            note="内邀能力未开放",
        )
        await store.flush()
        logs = await store.query_logs("capability", filters={"capability": "member_list"})
        assert logs["total"] == 1
        info = await store.db_info()
        assert info["tables"]["capability"]["count"] == 1
        assert info["tables"]["api"]["count"] == 0
        await store.close()

    run(scenario())


def test_prune_and_clear(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        store.record_api_call(group_id="g1", method="GET", path="/x", ok=True)
        await store.flush()
        old = 1  # 直接把 ts 改成很久以前
        store.enqueue(
            "api",
            {
                "group_id": "g1",
                "method": "GET",
                "path": "/old",
                "ok": True,
                "ts_unix": 1000,
            },
        )
        await store.flush()
        deleted = await store.prune({"api": 1})
        assert deleted["api"] >= 1
        remaining = await store.query_logs("api")
        assert remaining["total"] == 1
        cleared = await store.clear("all")
        assert cleared["api"] == 1
        assert (await store.query_logs("api"))["total"] == 0
        del old
        await store.close()

    run(scenario())


def test_mutes_lifecycle(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        await store.upsert_mute(
            group_id="g1",
            member_openid="u1",
            username="张三",
            until_unix=99_999_999_999,
            reason="广告",
            source="auto",
        )
        rows = await store.list_mutes("g1")
        assert len(rows) == 1 and rows[0]["username"] == "张三"
        await store.set_mute_active("g1", "u1", False)
        assert await store.list_mutes("g1") == []
        assert len(await store.list_mutes("g1", active_only=False)) == 1
        await store.close()

    run(scenario())


def test_queue_overflow_counts_dropped(tmp_path):
    async def scenario():
        store = await make_store(tmp_path, queue_maxsize=10)
        for index in range(50):
            store.enqueue("api", {"method": "GET", "path": f"/p{index}", "ok": True})
        assert store.stats["dropped"] > 0
        await store.flush()
        await store.close()

    run(scenario())


def test_join_profile_columns_written_and_read(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        await store.record_join(
            join_request_id="j1",
            group_id="g1",
            member_openid="10001",
            username="张三",
            decision="decline",
            decided_by="rule",
            profile_source="onebot_stranger",
            avatar_url="https://q1.qlogo.cn/g?b=qq&nk=10001&s=640",
            qq_level=16,
            account_age_days=3,
            reg_time=1700000000,
            qid="qid-1",
            profile_json='{"qq_level": 16}',
            gate="account_age",
        )
        await store.flush()
        row = await store.get_join("j1")
        assert row is not None
        assert row["qq_level"] == 16
        assert row["account_age_days"] == 3
        assert row["reg_time"] == 1700000000
        assert row["qid"] == "qid-1"
        assert row["profile_source"] == "onebot_stranger"
        assert row["avatar_url"].endswith("nk=10001&s=640")
        assert row["gate"] == "account_age"
        await store.close()

    run(scenario())


def test_join_profile_migration_is_idempotent(tmp_path):
    async def scenario():
        path = tmp_path / "audit.db"
        for _ in range(2):
            store = AuditStore(path, flush_interval=0.05)
            await store.initialize()
            await store.close()

    run(scenario())


def test_backup_creates_file(tmp_path):
    async def scenario():
        store = await make_store(tmp_path)
        store.record_api_call(group_id="g1", method="GET", path="/x", ok=True)
        await store.flush()
        target = tmp_path / "backup.db"
        await store.backup(target)
        assert target.exists() and target.stat().st_size > 0
        await store.close()

    run(scenario())
