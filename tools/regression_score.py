#!/usr/bin/env python3
"""回归集评分：把「人工标注清单 + 新判词结果」算成误杀/漏杀与一致性。

用法：
  python tools/regression_score.py --manifest tests/regression/image_manifest.tsv --results /tmp/new_verdicts.tsv
  # results.tsv 两列（制表符分隔，可含表头）：sample_id <TAB> new_verdict[<TAB> new_severity]

评分口径：
  · 误杀率 = 人工标 allow 但新判词 violation 的比例（越低越好，回归集 D 的红线）
  · 漏杀率 = 人工标 violation 但新判词非 violation 的比例
  · 一致率与 Cohen kappa：清单里若有两列人工标注（human_label / human_label_2）则一并给出
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys

TAB = chr(9)
LABELS = ("allow", "review", "violation")


def read_tsv(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle, delimiter=TAB)]


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    """两标注人的 Cohen kappa；样本为空或完全一致时返回 1.0/0.0 的合理值。"""
    pairs = [(str(a), str(b)) for a, b in pairs if a and b]
    if not pairs:
        return 0.0
    total = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / total
    expected = 0.0
    for label in LABELS:
        rate_a = sum(1 for a, _b in pairs if a == label) / total
        rate_b = sum(1 for _a, b in pairs if b == label) / total
        expected += rate_a * rate_b
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


def score(manifest: list[dict], results: dict[str, str]) -> dict:
    """manifest 行需含 human_label；results 为 sample_id -> new_verdict。"""
    scored = []
    for row in manifest:
        truth = str(row.get("human_label") or "").strip().lower()
        sample_id = str((row.get("sample_id") or row.get("id")) or "").strip()
        predicted = str(results.get(sample_id) or "").strip().lower()
        if truth not in LABELS or not predicted:
            continue
        scored.append((truth, predicted))

    positives = [pair for pair in scored if pair[0] == "violation"]
    negatives = [pair for pair in scored if pair[0] == "allow"]
    false_positive = sum(1 for truth, pred in negatives if pred == "violation")
    false_negative = sum(1 for truth, pred in positives if pred != "violation")
    agree = sum(1 for truth, pred in scored if truth == pred)
    return {
        "scored": len(scored),
        "agreement": round(agree / len(scored), 4) if scored else 0.0,
        "false_positive_rate": round(false_positive / len(negatives), 4) if negatives else 0.0,
        "false_negative_rate": round(false_negative / len(positives), 4) if positives else 0.0,
        "negatives": len(negatives),
        "positives": len(positives),
        "false_positive": false_positive,
        "false_negative": false_negative,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="回归集评分（误杀/漏杀/一致性）")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--max-false-positive-rate", type=float, default=0.05)
    args = parser.parse_args(argv)

    manifest = read_tsv(pathlib.Path(args.manifest))
    rows = read_tsv(pathlib.Path(args.results))
    results = {}
    for row in rows:
        sample_id = str((row.get("sample_id") or row.get("id")) or "").strip()
        verdict = str(row.get("new_verdict") or "").strip().lower()
        if sample_id:
            results[sample_id] = verdict

    report = score(manifest, results)
    kappa = cohen_kappa([(str(r.get("human_label") or ""), str(r.get("human_label_2") or "")) for r in manifest])
    if not report.get("scored"):
        print("  [FAIL] 没有可评分的样本：manifest 需要 human_label（人工标注），"
              "results 需要 sample_id/new_verdict 两列。少任何一列都会得到「全 0」的假结论。")
        return 2

    print(f"已评分 {report[chr(115)+chr(99)+chr(111)+chr(114)+chr(101)+chr(100)]} 条（负例 {report[chr(110)+chr(101)+chr(103)+chr(97)+chr(116)+chr(105)+chr(118)+chr(101)+chr(115)]} / 正例 {report[chr(112)+chr(111)+chr(115)+chr(105)+chr(116)+chr(105)+chr(118)+chr(101)+chr(115)]}）")
    print(f"误杀率 {report[chr(102)+chr(97)+chr(108)+chr(115)+chr(101)+chr(95)+chr(112)+chr(111)+chr(115)+chr(105)+chr(116)+chr(105)+chr(118)+chr(101)+chr(95)+chr(114)+chr(97)+chr(116)+chr(101)]:.2%}；漏杀率 {report[chr(102)+chr(97)+chr(108)+chr(115)+chr(101)+chr(95)+chr(110)+chr(101)+chr(103)+chr(97)+chr(116)+chr(105)+chr(118)+chr(101)+chr(95)+chr(114)+chr(97)+chr(116)+chr(101)]:.2%}；一致率 {report[chr(97)+chr(103)+chr(114)+chr(101)+chr(101)+chr(109)+chr(101)+chr(110)+chr(116)]:.2%}")
    print(f"两标注人 kappa = {kappa:.3f}（清单里没有 human_label_2 时不可信）")
    fps = report["false_positive_rate"]
    return 0 if fps <= args.max_false_positive_rate else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
