#!/usr/bin/env python3
"""导出内置规则本体（只读）。

用法：
  python tools/export_rules.py --out /tmp/rules.json
  python tools/export_rules.py --format text
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.rules import export_inventory


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="导出内置规则本体（只读）")
    parser.add_argument("--out", default="")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)

    inventory = export_inventory()
    if args.format == "json":
        payload = json.dumps(inventory, ensure_ascii=False, indent=2, default=str)
    else:
        chunks: list[str] = []
        for name, value in inventory.items():
            chunks.append(f"== {name}（{len(value)} 项）")
            if isinstance(value, dict):
                for key, item in value.items():
                    chunks.append(f"  {key}: {item}")
            else:
                for item in value:
                    chunks.append(f"  - {item}")
        payload = chr(10).join(chunks)

    if args.out:
        pathlib.Path(args.out).write_text(payload, encoding="utf-8")
        print(f"已导出 {len(inventory)} 组规则 -> {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
