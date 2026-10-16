"""面板渲染级冒烟门禁：装了 chrome 就真跑一遍三个视图，没装则跳过。

为什么放在 pytest 里：静态字符串断言抓不到「渲染时抛异常导致整页空白」这类缺陷（0.15.4~0.15.6 的
入群审批页就漏了一个月）。这条用例把 tools/panel_smoke.py 接进 CI 门禁。
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


@pytest.mark.skipif(not _chrome(), reason="未安装 chrome：跳过面板渲染冒烟（安装后自动生效）")
def test_panel_renders_three_views_without_runtime_error():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "panel_smoke.py")],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
