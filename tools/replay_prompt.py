#!/usr/bin/env python3
"""离线回放：把历史消息文本按新提示词重放，对比新旧判定（不改生产、不写库）。

用法：
  # 1) 只导出样本（不调用模型），产物给人工评审
  python tools/replay_prompt.py export --db /path/moderation.db --out /tmp/replay_cases.tsv --limit 300

  # 2) 调用任意 OpenAI 兼容接口做回放（需要模型凭据）
  python tools/replay_prompt.py replay --db /path/moderation.db --cases /tmp/replay_cases.tsv \
      --endpoint https://api.example.com/v1/chat/completions --key sk-xxx --model qwen3.8-flash \
      --out /tmp/replay_result.tsv

说明：
  · 数据库一律只读打开（sqlite URI mode=ro），不写任何业务库；
  · 判定口径与线上一致：用 src/moderator.py 的 SYSTEM_PROMPT_DEFAULT 与 USER_TEMPLATE_DEFAULT；
  · 文本样本来自 mod_events 的 text_excerpt（用户正文）：导出文件**不得提交进仓库、不得上传**；
  · 纯图片消息不在文本回放范围内（见实现文档 §5.4：图片要用图片回归集，配合 tests/regression/image_manifest.tsv）。
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sqlite3
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.moderator import (  # noqa: E402  (脚本按需运行，导入放后不影响 CLI)
    SYSTEM_PROMPT_DEFAULT,
    USER_TEMPLATE_DEFAULT,
    ModerationRequest,
    extract_json_object,
    extract_kv_verdict,
)

FIELDS = ["id", "ts", "group_id", "verdict", "category", "severity", "confidence", "text_excerpt"]


def open_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def cmd_export(args: argparse.Namespace) -> int:
    conn = open_ro(args.db)
    rows = conn.execute(
        "select id, ts, group_id, verdict, category, severity, confidence, ifnull(text_excerpt, '') "
        "from mod_events where ifnull(text_excerpt, '') != '' and verdict in ('allow', 'review', 'violation') "
        "order by id desc limit ?",
        (int(args.limit),),
    ).fetchall()
    conn.close()
    out = pathlib.Path(args.out)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=chr(9))
        writer.writerow([*FIELDS, "new_verdict", "new_category", "new_severity", "new_confidence", "changed"])
        for row in rows:
            writer.writerow([*list(row), "", "", "", "", ""])
    print(f"已导出 {len(rows)} 条样本 -> {out}")
    return 0


def chat(endpoint: str, key: str, model: str, system_prompt: str, user_prompt: str, timeout: float) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return str(body["choices"][0]["message"]["content"])


def parse_reply(raw: str) -> dict:
    payload = extract_json_object(raw) or extract_kv_verdict(raw)
    return payload or {}


def cmd_replay(args: argparse.Namespace) -> int:
    cases = pathlib.Path(args.cases)
    out = pathlib.Path(args.out)
    changed = 0
    total = 0
    with cases.open(encoding="utf-8") as src, out.open("w", encoding="utf-8", newline="") as dst:
        reader = csv.DictReader(src, delimiter=chr(9))
        writer = csv.DictWriter(dst, fieldnames=list(reader.fieldnames or FIELDS), delimiter=chr(9))
        writer.writeheader()
        for row in reader:
            text = str(row.get("text_excerpt") or "")
            if not text:
                continue
            # 用插件自己的渲染函数，保证与线上口径一致（占位符全部由 ModerationRequest 填充）
            request = ModerationRequest(group_id="replay", text=text)
            user_prompt = request.render_user_prompt(USER_TEMPLATE_DEFAULT)
            try:
                raw = chat(args.endpoint, args.key, args.model, SYSTEM_PROMPT_DEFAULT, user_prompt, args.timeout)
            except Exception as exc:  # pragma: no cover - 网络/凭据问题按条记录，不中断
                row["new_verdict"] = f"ERROR:{type(exc).__name__}"
                writer.writerow(row)
                continue
            parsed = parse_reply(raw)
            row["new_verdict"] = str(parsed.get("verdict") or "")
            row["new_category"] = str(parsed.get("category") or "")
            row["new_severity"] = str(parsed.get("severity") or "")
            row["new_confidence"] = str(parsed.get("confidence") or "")
            row["changed"] = "1" if row["new_verdict"] != row.get("verdict") else "0"
            total += 1
            if row["changed"] == "1":
                changed += 1
            writer.writerow(row)
    print(f"回放 {total} 条，判定变化 {changed} 条 -> {out}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="历史消息离线回放（只读）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="导出文本样本")
    p_export.add_argument("--db", required=True)
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--limit", type=int, default=200)
    p_export.set_defaults(func=cmd_export)

    p_replay = sub.add_parser("replay", help="按新提示词回放（需要模型凭据）")
    p_replay.add_argument("--cases", required=True)
    p_replay.add_argument("--out", required=True)
    p_replay.add_argument("--endpoint", required=True)
    p_replay.add_argument("--key", required=True)
    p_replay.add_argument("--model", required=True)
    p_replay.add_argument("--timeout", type=float, default=30.0)
    p_replay.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
