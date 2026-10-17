"""菜单图片渲染冒烟：装了 chrome 就真出一张菜单图，没装则跳过。

为什么要真出图：tests/test_menu_image.py 只断言 HTML 字符串，CSS 写坏、卡片被裁、
样式格式化报错这类问题在字符串层看不出来（0.15.16 换风格后正是这类风险）。

做法：菜单 HTML + 探针脚本 → chrome 无头 --dump-dom 读回「卡片宽 / 文档宽 / 文档高」→
      chrome 无头 --screenshot 出 PNG → 校验 PNG 头的宽高与文件大小。

断言：宽视口下卡片正好 1080px；窄视口下卡片自适应收缩且无横向溢出；出图有宽度、不是空白。

用法：python3 tools/menu_render_smoke.py [--chrome PATH] [--out /tmp/menu_render.png]
退出码：0 = 通过或跳过（无 chrome）；1 = 失败。
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def _menu_html() -> str:
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    render = importlib.import_module(f"{ROOT.name}.src.menu_render")
    return render.menu_html(
        version="0.0.0-smoke",
        requested_at="2026-10-18 04:00",
        requester="冒烟",
        group_name="冒烟群",
    )


PROBE = (
    "<script>window.addEventListener('load', function () {"
    "  var page = document.querySelector('.page');"
    "  var d = document.documentElement;"
    "  document.title = 'PROBE ' + Math.round(page.getBoundingClientRect().width) +'x'+ d.scrollWidth +'x'+ d.scrollHeight;"
    "});</script>"
)


def _run(chrome: str, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
           "--hide-scrollbars", "--virtual-time-budget=3000", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    return struct.unpack(">II", data[16:24])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    chrome = chrome_path(args.chrome)
    if not chrome:
        print("SKIP：未找到 chrome，跳过菜单出图冒烟（安装 chrome 或设置 CHROME 后重试）")
        return 0
    tmpdir = Path(tempfile.mkdtemp(prefix="menu-smoke-"))
    page = tmpdir / "menu.html"
    page.write_text(_menu_html() + PROBE, encoding="utf-8")
    png = Path(args.out) if args.out else tmpdir / "menu.png"
    url = page.as_uri()

    def probe(width: int, height: int = 1400) -> tuple[int, int, int]:
        dom = _run(chrome, [f"--window-size={width},{height}", "--dump-dom", url])
        found = re.search(r"<title>PROBE (\d+)x(\d+)x(\d+)</title>", dom.stdout)
        if not found:
            raise SystemExit("FAIL：探针没读到尺寸\n" + dom.stdout[-600:])
        return tuple(int(group) for group in found.groups())

    wide_card, _, doc_h = probe(1280)
    narrow_card, narrow_doc, _ = probe(760)
    _run(chrome, [f"--window-size={max(wide_card, 1080)},{min(max(doc_h, 800), 6000)}",
                  f"--screenshot={png}", url])
    if not png.is_file():
        print("FAIL：没有生成 PNG")
        return 1
    png_w, png_h = _png_size(png)
    size_kb = png.stat().st_size // 1024
    problems = []
    if wide_card != 1080:
        problems.append(f"宽视口下卡片宽度应为 1080，实际 {wide_card}")
    if narrow_card > 760:
        problems.append(f"窄视口下卡片没自适应收缩：{narrow_card} > 760")
    if narrow_doc > 761:
        problems.append(f"窄视口下出现横向溢出：文档宽 {narrow_doc}")
    if not 800 <= doc_h <= 6000:
        problems.append(f"文档高度异常：{doc_h}")
    if png_w < 1000:
        problems.append(f"出图宽度过窄：{png_w}")
    if size_kb < 20:
        problems.append(f"出图疑似空白：仅 {size_kb}KB")
    print(f"菜单出图：卡片宽 宽视口 {wide_card} / 窄视口 {narrow_card}（文档 {narrow_doc}）| 高 {doc_h} | PNG {png_w}x{png_h} | {size_kb}KB")
    if problems:
        for item in problems:
            print("FAIL：" + item)
        return 1
    print("OK：菜单出图冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
