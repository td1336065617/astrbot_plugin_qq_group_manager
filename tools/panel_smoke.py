#!/usr/bin/env python3
"""面板渲染级冒烟：真浏览器加载策略/入群/总览三视图，断言渲染出关键内容且无运行时错误。

为什么需要它：tests/ 里的面板测试全是静态字符串断言，抓不到「渲染时抛异常导致整页空白」这类缺陷
（0.15.4~0.15.6 的入群审批页正是这样躺了一个月）。本脚本用 chrome 无头模式 --dump-dom 真跑一遍。

用法：python3 tools/panel_smoke.py [--keep] [--chrome PATH]
退出码：0 = 全部通过或跳过（无 chrome）；1 = 有视图渲染失败。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import http.server
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "tools" / "panel_preview"

ROUTES = [
    # (视图, 名称, 必须出现的内容, 不该出现的内部术语, DOM 体积下限：整页崩了会明显变小)
    ("dashboard", "总览", ["判定构成", "运行状态", "可用性"], ["KV 兜底", "umo", "err_code=", "stage 口径", "爱莉希雅"], 2500),
    ("policy", "策略", ["主模型（发言审核）", "备用模型", "送审条件", "重复违规时长累加"], ["兜底处置", "动作地板", "爱莉希雅"], 6000),
    # 入群页断言必须落在本群规则卡内：顶栏就有"入群审批"，拿它当断言会漏掉整页崩溃
    ("joins", "入群审批", ["本群规则", "跟随全局配置（不含审批模式）", "待人工审批"], ["兜底处置", "动作地板", "爱莉希雅"], 6000),
]


def chrome_path(explicit: str = "") -> str:
    if explicit:
        return explicit
    env = os.environ.get("CHROME") or os.environ.get("CHROME_PATH")
    if env:
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def build_config() -> dict:
    sys.path.insert(0, str(ROOT))
    from src.models import (  # noqa: PLC0415
        CAPABILITIES,
        CAPABILITY_LABELS,
        JOIN_GLOBAL_ONLY_KEYS,
        JOIN_GROUP_OVERRIDABLE_KEYS,
        JOIN_REVIEW_MODES,
        MODERATION_MODES,
        SEND_CONDITIONS,
        default_settings,
    )

    settings = default_settings()
    settings["llm_fallback_provider_ids"] = ["gone/stale", "openai/gpt-4o-mini"]
    settings["join_llm_fallback_provider_ids"] = ["gone/stale"]
    return {
        "settings": settings,
        "groups": [{"group_id": "g1", "name": "预览群", "join_review_mode": "", "join_follow_global": False, "join_overrides": {"join_llm_fallback_provider_ids": ["gone/stale"]}}],
        "providers": {
            "items": [
                {"id": "openai/gpt-4o-mini", "model": "gpt-4o-mini", "type": "openai"},
                {"id": "deepseek/deepseek-chat", "model": "deepseek-chat", "type": "deepseek"},
                {"id": "dup/one", "model": "qwen-max", "type": "openai"},
                {"id": "dup/two", "model": "qwen-max", "type": "dashscope"},
            ],
            "configured": "openai/gpt-4o-mini",
            "last_used": "openai/gpt-4o-mini",
        },
        "options": {
            "modes": list(MODERATION_MODES),
            "join_modes": list(JOIN_REVIEW_MODES),
            "send_conditions": list(SEND_CONDITIONS),
            "capabilities": list(CAPABILITIES),
            "capability_labels": dict(CAPABILITY_LABELS),
            "join_overridable_keys": list(JOIN_GROUP_OVERRIDABLE_KEYS),
            "join_global_only_keys": list(JOIN_GLOBAL_ONLY_KEYS),
            "default_settings": default_settings(),
        },
        "ui_state": {},
        "runtime": {"transport": {"available": True, "platform_id": "preview"}, "dry_run": True, "groups_total": 1, "groups_moderating": 1, "mode": "standard"},
    }


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, *a):  # 静默
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def dump_dom(chrome: str, url: str, timeout: int = 60) -> str:
    cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
           "--virtual-time-budget=6000", "--dump-dom", url]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return out.stdout or ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    chrome = chrome_path(args.chrome)
    if not chrome:
        print("SKIP：未找到 chrome，跳过面板渲染冒烟（安装 chrome 或设置 CHROME 后重试）")
        return 0

    work = Path(tempfile.mkdtemp(prefix="panel_smoke_"))
    try:
        for name in ("app.js", "panel_logic.js", "style.css"):
            shutil.copy2(ROOT / "pages" / "manage" / name, work / name)
        for name in ("index.html", "errhook.js", "bridge_stub.js"):
            shutil.copy2(PREVIEW / name, work / name)
        (work / "config.json").write_text(json.dumps(build_config(), ensure_ascii=False), encoding="utf-8")
        httpd, port = serve(work)
        failures = []
        for view, label, must, forbidden, min_bytes in ROUTES:
            url = f"http://127.0.0.1:{port}/index.html#/{view}"
            dom = dump_dom(chrome, url)
            missing = [m for m in must if m not in dom]
            if "__errhook" in dom:
                import re as _re
                m = _re.search(r"<div id=\"__errhook\"[^>]*>(.*?)</div>", dom, _re.S)
                if m and m.group(1).strip():
                    missing.append("JS 运行时错误: " + m.group(1).strip()[:160])
            if len(dom) < min_bytes:
                missing.append(f"DOM 体积异常小（{len(dom)} < {min_bytes} 字节，疑似整页渲染中断）")
            jargon = [f for f in forbidden if f in dom]
            status = "OK" if not missing and not jargon else "FAIL"
            if status == "FAIL":
                failures.append((label, missing, jargon, len(dom)))
            print(f"  [{status}] {label}：DOM {len(dom)} 字节"
                  + (f"｜缺少关键内容 {missing}" if missing else "")
                  + (f"｜不该出现的内部术语 {jargon}" if jargon else ""))
        httpd.shutdown()
        if failures:
            print(f"[FAIL] {len(failures)} 个视图渲染异常（多半是渲染时抛了异常导致内容没出来）")
            return 1
        print("  [OK] 三个视图渲染冒烟通过（真浏览器）")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("保留目录：", work)


if __name__ == "__main__":
    raise SystemExit(main())
