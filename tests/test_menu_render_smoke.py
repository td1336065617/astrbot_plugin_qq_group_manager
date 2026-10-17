"""菜单出图冒烟门禁：装了 chrome 就真出一张菜单图，没装则跳过。

为什么放 pytest 里：字符串断言抓不到「CSS 写坏 / 卡片被裁 / 样式格式化报错」。
0.15.16 换菜单风格、0.15.17 修文案之后，这类风险只看 HTML 是看不出来的。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _chrome() -> str:
    env = os.environ.get("CHROME") or os.environ.get("CHROME_PATH")
    if env:
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def test_menu_render_smoke() -> None:
    chrome = _chrome()
    if not chrome:
        pytest.skip("未找到 chrome，跳过菜单出图冒烟")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "menu_render_smoke.py"), "--chrome", chrome],
        capture_output=True,
        text=True,
        timeout=240,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, output[-1500:]
    assert "OK：菜单出图冒烟通过" in output, output[-800:]

