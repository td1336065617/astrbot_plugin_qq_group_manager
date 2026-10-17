"""store_text 的体积影响实测（只写临时库，不碰生产）。

用法：python3 tools/store_text_impact.py [--rows 2000]
输出：120 字摘要 vs 4000 字全文两种模式下 mod_events 的体积与单条均值，用于评估开启 store_text 的代价。
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit import AuditStore


def _fill(db: Path, rows: int, chars: int) -> int:
    conn = sqlite3.connect(db)
    columns = [(row[1], row[2], row[3], row[4]) for row in conn.execute("PRAGMA table_info(mod_events)")]
    text = "加" * chars
    fields = {}
    for name, coltype, notnull, dflt in columns:
        if name == "text_excerpt":
            fields[name] = text
        elif notnull and dflt is None:
            fields[name] = 0 if "INT" in str(coltype).upper() else "x"
    names = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.executemany(
        f"insert into mod_events ({names}) values ({placeholders})",
        [tuple(fields.values()) for _ in range(rows)],
    )
    conn.commit()
    conn.execute("vacuum")
    conn.close()
    return db.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="store_text 体积影响实测")
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--export-limit", type=int, default=20000)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="store_text_impact_") as tmp:
        sizes = {}
        for label, chars, name in (("摘要(120字)", 120, "a.db"), ("全文(4000字)", 4000, "b.db")):
            db = Path(tmp) / name
            asyncio.run(AuditStore(str(db)).initialize())
            sizes[label] = _fill(db, args.rows, chars)
        base, full = sizes["摘要(120字)"], sizes["全文(4000字)"]
        print(f"样本 {args.rows} 条事件（CJK 每字 3 字节）")
        for label, size in sizes.items():
            print(f"  {label}: {size / 1024:.1f} KB（单条约 {size / args.rows:.0f} 字节）")
        ratio = full / max(base, 1)
        print(f"  开启 store_text 后约放大 {ratio:.1f} 倍；每 1000 条多约 {(full - base) / 1024 * 1000 / args.rows:.1f} KB")
        worst = (full / args.rows) * args.export_limit / 1024 / 1024
        print(f"  按导出上限 {args.export_limit} 行估算：开启后单次导出最坏约 {worst:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
