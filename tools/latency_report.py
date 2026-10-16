#!/usr/bin/env python3
"""判定延迟与可用性报表（只读）。

用法：
  python tools/latency_report.py --db <moderation.db> [--since 24h|7d|<ISO>] [--min-samples N] [--json]
阈值默认：超时率 <= 5%；判定成功率 >= 95%；成功判定 p95 延迟 <= 20000ms。
退出码：0 = 阈值全过；1 = 有阈值未过；2 = 样本不足（--min-samples），便于定时任务告警。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone
import sys

CN_TZ = timezone(timedelta(hours=8))
FAIL_PREFIX = "模型调用失败"
EMPTY_SQL = chr(39) + chr(39)


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, round(ratio * (len(values) - 1))))
    return int(values[index])


def summarize(rows: list[tuple], max_timeout_rate: float = 0.05, max_p95_ms: int = 20000) -> dict:
    """rows = [(verdict, reason, latency_ms)]；纯函数，便于单测。"""
    total = len(rows)
    failed = sum(1 for _v, reason, _l in rows if str(reason or "").startswith(FAIL_PREFIX))
    latencies = sorted(
        int(lat or 0)
        for _v, reason, lat in rows
        if not str(reason or "").startswith(FAIL_PREFIX) and int(lat or 0) > 0
    )
    judged = total - failed
    success_rate = 1.0 if total == 0 else judged / total
    timeout_rate = 0.0 if total == 0 else failed / total
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    p99 = _percentile(latencies, 0.99)
    checks = {
        "timeout_rate": (timeout_rate <= max_timeout_rate, round(timeout_rate, 4), max_timeout_rate),
        "success_rate": (success_rate >= 1 - max_timeout_rate, round(success_rate, 4), 1 - max_timeout_rate),
        "p95_ms": (p95 <= max_p95_ms, p95, max_p95_ms),
    }
    return {
        "total": total,
        "judged": judged,
        "failed": failed,
        "success_rate": round(success_rate, 4),
        "timeout_rate": round(timeout_rate, 4),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "passed": all(item[0] for item in checks.values()),
        "checks": {key: {"ok": item[0], "value": item[1], "limit": item[2]} for key, item in checks.items()},
    }


def _resolve_since(value: str) -> str:
    """把 24h / 7d / 90m 这类简写换算成北京时间的 ISO 串；ISO 原样返回。"""
    text = str(value or "").strip()
    unit = text[-1:].lower()
    if unit in ("h", "d", "m") and text[:-1].isdigit():
        minutes = int(text[:-1]) * {"h": 60, "d": 1440, "m": 1}[unit]
        moment = datetime.now(CN_TZ) - timedelta(minutes=minutes)
        return moment.isoformat(timespec="seconds")
    return text

def hourly(db: pathlib.Path, since: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "select substr(ts, 1, 13) as hour, count(*), "
            "sum(case when reason like ? then 1 else 0 end) as failed, ifnull(max(latency_ms), 0) "
            "from mod_events where ts >= ? group by 1 order by 1",
            (FAIL_PREFIX + "%", since),
        ).fetchall()
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="判定延迟与可用性报表（只读）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--since", default="1970-01-01T00:00:00+08:00")
    parser.add_argument("--min-samples", type=int, default=0, help="样本少于此数直接判失败（0=不检查），避免空窗口静默判 OK")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-timeout-rate", type=float, default=0.05)
    parser.add_argument("--max-p95-ms", type=int, default=20000)
    args = parser.parse_args(argv)

    db = pathlib.Path(args.db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select verdict, ifnull(reason, " + EMPTY_SQL + "), ifnull(latency_ms, 0) from mod_events where ts >= ?",
            (_resolve_since(args.since),),
        ).fetchall()
    finally:
        conn.close()

    report = summarize([tuple(row) for row in rows], args.max_timeout_rate, args.max_p95_ms)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1

    total, judged, failed = report["total"], report["judged"], report["failed"]
    if total == 0:
        print("  [WARN] 本窗口没有任何判定样本：门禁没有实际生效，请检查 --since 口径。")
    if args.min_samples and total < args.min_samples:
        print(f"  [FAIL] 样本不足：{total} < {args.min_samples}")
        return 2
    success, timeout = report["success_rate"] * 100, report["timeout_rate"] * 100
    p50, p95, p99 = report["p50_ms"], report["p95_ms"], report["p99_ms"]
    print(f"样本 {total} 条：判定成功 {judged} / 失败 {failed}")
    print(f"判定成功率 {success:.2f}%；超时率 {timeout:.2f}%")
    print(f"延迟 p50 {p50}ms / p95 {p95}ms / p99 {p99}ms")
    for key, item in report["checks"].items():
        flag = "OK  " if item["ok"] else "FAIL"
        print(f"  [{flag}] {key}: {item[chr(118)+chr(97)+chr(108)+chr(117)+chr(101)]} (限 {item[chr(108)+chr(105)+chr(109)+chr(105)+chr(116)]})")
    print("逐小时（时 / 总数 / 失败 / 最大延迟ms）：")
    for row in hourly(db, _resolve_since(args.since)):
        print("  ", row)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
