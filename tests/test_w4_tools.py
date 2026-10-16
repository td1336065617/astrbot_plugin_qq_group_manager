"""W4：验收闭环工具（延迟报表 / 回归集评分）单测。"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.latency_report import summarize
from tools.regression_score import cohen_kappa, score


def test_latency_summarize_detects_timeout_rate():
    rows = [("review", "模型调用失败：TimeoutError", 0)]
    rows += [("allow", "正常", 1000 * (i + 1)) for i in range(9)]
    report = summarize(rows, max_timeout_rate=0.05, max_p95_ms=20000)
    assert report["total"] == 10 and report["failed"] == 1
    assert report["timeout_rate"] == 0.1
    assert report["success_rate"] == 0.9
    assert report["passed"] is False
    assert report["p50_ms"] > 0 and report["p95_ms"] >= report["p50_ms"]


def test_latency_summarize_passes_healthy_window():
    rows = [("allow", "正常", 1200) for _ in range(50)] + [("review", "模型调用失败：TimeoutError", 0)]
    report = summarize(rows, max_timeout_rate=0.05, max_p95_ms=20000)
    assert report["passed"] is True
    assert report["p95_ms"] == 1200


def test_latency_summarize_handles_empty():
    report = summarize([])
    assert report["total"] == 0 and report["passed"] is True
    assert report["p50_ms"] == 0 and report["timeout_rate"] == 0.0


def test_score_reports_false_positive_and_false_negative():
    manifest = [
        {"sample_id": "1", "human_label": "allow"},
        {"sample_id": "2", "human_label": "allow"},
        {"sample_id": "3", "human_label": "violation"},
        {"sample_id": "4", "human_label": "violation"},
        {"sample_id": "5", "human_label": ""},
    ]
    results = {"1": "allow", "2": "violation", "3": "violation", "4": "review"}
    report = score(manifest, results)
    assert report["scored"] == 4
    assert report["false_positive"] == 1 and report["false_positive_rate"] == 0.5
    assert report["false_negative"] == 1 and report["false_negative_rate"] == 0.5
    assert report["agreement"] == 0.5


def test_cohen_kappa_boundaries():
    assert cohen_kappa([]) == 0.0
    assert cohen_kappa([("allow", "allow"), ("violation", "violation")]) == 1.0
    assert cohen_kappa([("allow", "violation"), ("violation", "allow")]) < 0
    mid = cohen_kappa([("allow", "allow"), ("allow", "violation"), ("violation", "violation"), ("violation", "violation")])
    assert 0.0 < mid < 1.0


def test_resolve_since_accepts_shorthand():
    "运维最容易踩的坑：把 --since 24 当 ISO 传，窗口空掉却静默判 OK。"
    from datetime import datetime, timedelta, timezone

    from tools.latency_report import _resolve_since

    cn = timezone(timedelta(hours=8))
    resolved = datetime.fromisoformat(_resolve_since("24h"))
    delta = datetime.now(cn) - resolved
    assert timedelta(hours=23, minutes=30) < delta < timedelta(hours=24, minutes=30), delta
    assert datetime.fromisoformat(_resolve_since("7d")) < datetime.now(cn) - timedelta(days=6)
    iso = "2026-10-15T20:41:00+08:00"
    assert _resolve_since(iso) == iso


def test_min_samples_fails_on_empty_window(tmp_path, capsys):
    "空窗口必须判失败，而不是打印一堆 OK。"
    import sqlite3

    from tools.latency_report import main

    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("create table mod_events (ts text, verdict text, reason text, latency_ms integer)")
    conn.commit()
    conn.close()

    code = main(["--db", str(db), "--since", "24h", "--min-samples", "1"])
    out = capsys.readouterr().out
    assert code == 2
    assert "WARN" in out and "样本不足" in out
