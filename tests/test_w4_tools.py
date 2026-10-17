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


def _acc_db(tmp_path, name, rows, mutes=None, api=None, cap=None):
    import sqlite3

    db = tmp_path / name
    conn = sqlite3.connect(db)
    conn.execute(
        "create table mod_events (ts text, stage text, verdict text, fallback_index int, latency_ms int, provider_id text)"
    )
    conn.execute("create table mutes (event_id int, updated_unix int)")
    conn.execute("create table api_calls (ok int, err_code int, ts_unix int)")
    conn.execute("create table capability_log (capability text, ok int, err_code int, ts_unix int, note text)")
    conn.execute("create table join_requests (decision text)")
    for row in rows:
        conn.execute("insert into mod_events values (?,?,?,?,?,?)", tuple(row) + (None,) * (6 - len(row)))
    for row in mutes or []:
        conn.execute("insert into mutes values (?,?)", row)
    for row in api or []:
        conn.execute("insert into api_calls values (?,?,?)", row)
    for row in cap or []:
        padded = tuple(row) + (None,) * (5 - len(row))
        conn.execute("insert into capability_log values (?,?,?,?,?)", padded)
    conn.execute("insert into join_requests values (?)", ("pending",))
    conn.commit()
    conn.close()
    return db


DEPLOY = "2026-10-16T20:41:00+08:00"
#: 部署后 10 分钟（必须晚于 DEPLOY，否则会被窗口正确排除——这正是工具的设计）
import datetime as _dt  # noqa: E402 - 需要放在窗口常量之后才可读

POST_TS = int(_dt.datetime.fromisoformat(DEPLOY).timestamp()) + 600


def test_acceptance_report_passes_on_clean_data(tmp_path):
    from tools.acceptance_report import build_report

    db = _acc_db(
        tmp_path,
        "ok.db",
        [("2026-10-16T21:00:00+08:00", "judged", "allow", 0, 9000)],
        mutes=[(7, POST_TS)],
        api=[(1, 0, POST_TS)],
    )
    report = build_report(db, DEPLOY)
    assert report["passed"] is True
    assert report["post"]["events"] == 1


def test_acceptance_report_flags_missing_err_code_and_event_id(tmp_path):
    "部署后仍出现 NULL 错误码 / 禁言缺 event_id 时必须判失败（N1/N2/N4 的机器化护栏）。"
    from tools.acceptance_report import build_report

    db = _acc_db(
        tmp_path,
        "bad.db",
        [("2026-10-16T21:00:00+08:00", "judged", "allow", 0, 9000)],
        mutes=[(None, POST_TS)],
        api=[(0, None, POST_TS)],
        cap=[("llm_judge", 0, None, POST_TS)],
    )
    report = build_report(db, DEPLOY)
    assert report["passed"] is False
    failed = {item["id"] for item in report["checks"] if not item["ok"]}
    assert failed == {"A", "B", "C"}, failed


def test_acceptance_report_flags_unknown_stage(tmp_path):
    from tools.acceptance_report import build_report

    db = _acc_db(tmp_path, "stage.db", [("2026-10-16T21:00:00+08:00", "judeged", "allow", 0, 1)])
    report = build_report(db, DEPLOY)
    assert report["passed"] is False
    assert any(item["id"] == "D" and not item["ok"] for item in report["checks"])


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_regression_score_fails_loudly_when_unlabelled(tmp_path, capsys):
    "未标注的清单（回放导出的原始产物）不得给出「全 0」的假结论，必须判失败。"
    from tools.regression_score import main

    manifest = _write(tmp_path / "m.tsv", "id\tverdict\n1\tallow\n")
    results = _write(tmp_path / "r.tsv", "sample_id\tnew_verdict\n1\tallow\n")
    code = main(["--manifest", str(manifest), "--results", str(results)])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "FAIL" in out


def test_regression_score_accepts_id_keyed_results(tmp_path, capsys):
    "回放导出用 id；评分器必须能直接串起来用（这是两个交付件之间的真实集成点）。"
    from tools.regression_score import main

    manifest = _write(
        tmp_path / "m2.tsv",
        "id\thuman_label\n1\tallow\n2\tviolation\n3\tallow\n",
    )
    results = _write(tmp_path / "r2.tsv", "id\tnew_verdict\n1\tallow\n2\tallow\n3\tallow\n")
    code = main(["--manifest", str(manifest), "--results", str(results)])
    out = capsys.readouterr().out
    assert "已评分 3 条" in out, out
    # 评分器只对「误杀率」设退出码门槛（误杀优先），漏杀只报告 —— 这里断言漏杀被正确算出来
    assert code == 0, out
    assert "漏杀率 100.00%" in out, out

def test_acceptance_report_accepts_capability_failure_with_note(tmp_path):
    """能力缺失类失败（接口仅白名单机器人可用等）天然没有 err_code，但有 note 说明 -> B 不应判失败。"""
    from tools.acceptance_report import build_report

    db = _acc_db(
        tmp_path,
        "note.db",
        [("2026-10-16T21:00:00+08:00", "judged", "allow", 0, 9000)],
        mutes=[(7, POST_TS)],
        api=[(1, 0, POST_TS)],
        cap=[("blacklist", 0, None, POST_TS, "该接口仅白名单机器人可用")],
    )
    report = build_report(db, DEPLOY)
    assert report["passed"] is True, [i for i in report["checks"] if not i["ok"]]


def test_acceptance_report_includes_parse_error_rate(tmp_path):
    db = _acc_db(tmp_path, "rate.db", [
        ("2026-10-16T21:00:00+08:00", "parse_error", "review", 0, 100),
        ("2026-10-16T21:01:00+08:00", "judged", "allow", 0, 100),
    ])
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("update mod_events set provider_id = 'A'")
    conn.commit()
    conn.close()
    from tools.acceptance_report import build_report

    report = build_report(db, DEPLOY)
    assert report["parse_error_rate_by_provider"] == [["A", 1, 2]]
