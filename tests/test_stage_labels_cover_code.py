"""stage 词表与面板标签的**仓内**护栏（#12）。

为什么要有：tests/test_s3_stage_vocabulary.py 的 3 条文档护栏在 CI 会 skip（docs 不在仓库内），
于是「代码产出的 stage 都能在面板显示中文」这件事在 CI 实际没有护栏。
本文件只依赖仓库内文件：代码里的 stage 字面量 ∪ REAL_STAGES ⊆ 面板 STAGE_LABELS。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.audit import REAL_STAGES

ROOT = Path(__file__).resolve().parents[1]


def _panel_stage_labels() -> set[str]:
    app = (ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const STAGE_LABELS = \{(.*?)\n\};", app, re.S)
    assert block, "面板 app.js 里找不到 STAGE_LABELS"
    return set(re.findall(r"([a-z_]+):", block.group(1)))


def _code_stage_literals() -> set[str]:
    found: set[str] = set()
    for path in [ROOT / "main.py", *sorted((ROOT / "src").glob("*.py"))]:
        found |= set(re.findall(r'stage="([a-z_]+)"', path.read_text(encoding="utf-8")))
    return found


def test_every_stage_produced_by_code_has_a_panel_label():
    missing = sorted((_code_stage_literals() | set(REAL_STAGES)) - _panel_stage_labels())
    assert not missing, f"这些 stage 在面板没有中文标签，会被原样显示成英文：{missing}"


def test_real_stages_are_subset_of_panel_labels():
    assert set(REAL_STAGES) <= _panel_stage_labels()


def test_guard_has_teeth_when_a_label_is_missing():
    labels = _panel_stage_labels()
    assert "throttled" in labels, "throttled（单群 QPM 限流）会真的产出，必须在面板有标签"
