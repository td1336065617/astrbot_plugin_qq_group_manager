#!/usr/bin/env python3
"""生成图片回归集清单（只读、不含正文与图片本体）。

用法：
  python tools/gen_image_manifest.py --db /path/moderation.db --out tests/regression/image_manifest.tsv \
      --limit 200 --allow 120 --review-other 40

说明：
  · 数据库只读打开（sqlite URI mode=ro）；
  · 纯图片消息的判据：text_digest 等于空字符串的 sha256 前缀（生产为 39 字符的 sha256: + 32 位十六进制）；
  · 清单里的 verdict/category/severity/confidence 是**旧提示词的历史输出**（label_source=machine_old_prompt），
    只作为临时标签，必须由人工复核后才可作为回归集真值（见实现文档 §5.4/§16.2）；
  · 清单只含事件 id / 时间 / 群标识哈希 / 空图片指纹列 / 历史判定 / 空的人工标签列；**不含**用户正文、模型理由、明文群号与图片内容。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import pathlib
import sqlite3

IMAGE_DIGEST_PREFIX = "sha256:e3b0c44298fc1c149afbf4c8996fb924"
B_TRACK = ("色情低俗", "辱骂攻击", "政治敏感", "违法违规")
FIELDS = [
    "sample_id",
    "ts",
    "group_ref",
    "image_fingerprint",
    "history_verdict",
    "history_category",
    "history_severity",
    "history_confidence",
    "label_source",
    "human_label",
    "human_severity",
    "reviewer",
    "note",
]


def query(conn: sqlite3.Connection, where: str, limit: int) -> list:
    if limit <= 0:
        return []
    sql = (
        "select id, ts, group_id, verdict, category, severity, round(confidence, 2) from mod_events "
        "where text_digest like ? and " + where + " order by id desc limit ?"
    )
    return conn.execute(sql, (IMAGE_DIGEST_PREFIX + "%", int(limit))).fetchall()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成图片回归集清单（只读）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=200, help="总上限")
    parser.add_argument("--allow", type=int, default=120, help="正常样本（allow）上限")
    parser.add_argument("--review-other", type=int, default=40, help="review 且 category=无 的上限")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows: list = []
    rows += query(conn, "verdict = 'violation'", 40)
    rows += query(conn, "category in (" + ", ".join("'" + c + "'" for c in B_TRACK) + ")", 40)
    rows += query(conn, "verdict = 'review' and category = '无'", args.review_other)
    rows += query(conn, "verdict = 'allow'", args.allow)
    conn.close()

    seen: set = set()
    unique = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        unique.append(row)
        if len(unique) >= args.limit:
            break

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=chr(9))
        writer.writerow(FIELDS)
        for sample_id, ts, group_id, verdict, category, severity, confidence in unique:
            writer.writerow([
                sample_id,
                ts,
                "g_" + hashlib.sha256(str(group_id).encode("utf-8")).hexdigest()[:8],
                verdict,
                category,
                severity,
                confidence,
                "",  # image_fingerprint：取到图后填
                "machine_old_prompt",
                "",
                "",
                "",
                "",
            ])
    print(f"已生成 {len(unique)} 条图片样本 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
