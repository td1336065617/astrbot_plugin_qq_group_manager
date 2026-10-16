#!/usr/bin/env python3
"""批次 2 验收一键报告（只读）。

用法：
  python tools/acceptance_report.py --db <moderation.db> --since-deploy 2026-10-16T20:41:00+08:00 [--json]

设计要点：只有「部署后」的行才参与判定，部署前的行只作为对照展示 —— 这样 N1/N2/N4 那类
「新代码写出来的字段是不是真的非空」才能变成可机器判定的不变量，而不是靠人眼看。

判定项（全部只在部署后窗口生效）：
  A. 失败的平台调用必须带非空 err_code（api_calls.ok=0 -> err_code 非 NULL）
  B. 失败的能力调用必须带非空 err_code（capability_log.ok=0 -> err_code 非 NULL）
  C. 部署后新增/更新的禁言必须关联 event_id（mutes.event_id 非空）
  D. stage 必须落在已知词表内（真判定五类 + 降级九类）
  E. 部署后窗口若没有任何判定样本，打印 WARN（不判失败，避免夜里误报）

退出码：0 = 全过；1 = 有判定项未过（便于定时任务）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

REAL_STAGES = ("judged", "judged_kv", "rule", "flood", "manual")
DEGRADED_STAGES = (
    "budget",
    "sampled_out",
    "throttled",
    "api_error",
    "circuit_open",
    "no_provider",
    "parse_error",
    "low_confidence",
    "illegal_value",
)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, args).fetchall()]


def build_report(db: pathlib.Path, since_deploy: str, days: int = 1) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        post_events = _rows(
            conn,
            "select stage, verdict, fallback_index, latency_ms from mod_events where ts >= ?",
            (since_deploy,),
        )
        pre_events = _rows(
            conn,
            "select stage, count(*) from mod_events where ts < ? group by 1 order by 2 desc limit 6",
            (since_deploy,),
        )
        post_mutes_with_id = _rows(
            conn,
            "select count(*), sum(case when event_id is not null then 1 else 0 end) from mutes "
            "where updated_unix >= strftime(" + chr(39) + "%s" + chr(39) + ", ?)",
            (since_deploy,),
        )[0]
        post_api_fail = _rows(
            conn,
            "select count(*), sum(case when err_code is null then 1 else 0 end) from api_calls "
            "where ok = 0 and ts_unix >= strftime(" + chr(39) + "%s" + chr(39) + ", ?)",
            (since_deploy,),
        )[0]
        post_cap_fail = _rows(
            conn,
            "select count(*), sum(case when err_code is null then 1 else 0 end) from capability_log "
            "where ok = 0 and ts_unix >= strftime(" + chr(39) + "%s" + chr(39) + ", ?)",
            (since_deploy,),
        )[0]
        join_states = _rows(conn, "select decision, count(*) from join_requests group by 1 order by 2 desc")
    finally:
        conn.close()

    known = set(REAL_STAGES) | set(DEGRADED_STAGES)
    unknown_stages = sorted({str(row[0]) for row in post_events if str(row[0]) not in known})
    degraded = sum(1 for row in post_events if str(row[0]) not in REAL_STAGES)
    fallback_used = sum(1 for row in post_events if int(row[2] or 0) > 0)
    latencies = [int(row[3] or 0) for row in post_events]
    checks = [
        {
            "id": "A",
            "name": "部署后平台失败必须带 err_code",
            "ok": int(post_api_fail[1] or 0) == 0,
            "detail": f"失败 {int(post_api_fail[0] or 0)} 条，其中 err_code 为空 {int(post_api_fail[1] or 0)} 条",
        },
        {
            "id": "B",
            "name": "部署后能力失败必须带 err_code",
            "ok": int(post_cap_fail[1] or 0) == 0,
            "detail": f"失败 {int(post_cap_fail[0] or 0)} 条，其中 err_code 为空 {int(post_cap_fail[1] or 0)} 条",
        },
        {
            "id": "C",
            "name": "部署后禁言必须关联 event_id",
            "ok": int(post_mutes_with_id[0] or 0) == int(post_mutes_with_id[1] or 0),
            "detail": f"受影响 {int(post_mutes_with_id[0] or 0)} 行，带 event_id {int(post_mutes_with_id[1] or 0)} 行",
        },
        {
            "id": "D",
            "name": "stage 必须在已知词表内",
            "ok": not unknown_stages,
            "detail": ("未知 stage: " + ", ".join(unknown_stages)) if unknown_stages else "全部已知",
        },
    ]
    warnings = []
    if not post_events:
        warnings.append("部署后窗口没有判定样本：门禁未实际生效（可能只是没人说话）")
    return {
        "since_deploy": since_deploy,
        "post": {
            "events": len(post_events),
            "degraded": degraded,
            "fallback_used": fallback_used,
            "latency_max_ms": max(latencies) if latencies else 0,
            "stages": sorted({str(row[0]) for row in post_events}),
        },
        "pre_top_stages": [[str(r[0]), int(r[1])] for r in pre_events],
        "join_states": [[str(r[0]), int(r[1])] for r in join_states],
        "checks": checks,
        "warnings": warnings,
        "passed": all(bool(item["ok"]) for item in checks),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="批次 2 验收一键报告（只读）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--since-deploy", required=True)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(pathlib.Path(args.db), args.since_deploy, args.days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1

    post = report['post']
    print(f"部署后窗口 {report['since_deploy']}：事件 {post['events']} 条（降级 {post['degraded']}）")
    print(f"  备用模型生效次数 {post['fallback_used']}；最大延迟 {post['latency_max_ms']}ms；stage {post['stages']}")
    print(f"  部署前对照 stage top：{report['pre_top_stages']}")
    print(f"  入群申请：{report['join_states']}")
    for item in report['checks']:
        flag = "OK  " if item['ok'] else "FAIL"
        print(f"  [{flag}] {item['id']}. {item['name']}：{item['detail']}")
    for text in report['warnings']:
        print(f"  [WARN] {text}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
