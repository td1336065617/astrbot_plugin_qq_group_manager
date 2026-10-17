"""表头必须是可读的不透明背景（sticky + transparent 会在滚动时透出内容）。"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_sticky_header_has_opaque_background() -> None:
    css = (ROOT / "pages" / "manage" / "style.css").read_text(encoding="utf-8")
    match = re.search(r"th \{([^}]*)\}", css)
    assert match, "找不到 th 样式"
    body = match.group(1)
    assert "position: sticky" in body, "表头应为 sticky（改了就要同步更新这条守门）"
    assert "background: transparent" not in body, "sticky 表头不能是透明背景，否则滚动时与内容重叠"
    assert "var(--panel)" in body or "var(--bg)" in body, "表头需要不透明背景色"
    assert "z-index" in body, "sticky 表头需要 z-index，避免被行内容盖住"


def test_boundary_table_has_no_scroll_container() -> None:
    js = (ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    start = js.index("const boundaryBox = el(")
    chunk = js[start : start + 160]
    assert "table-wrap" not in chunk, "9 行的处罚边界表不要放滚动容器（否则表头滚动错位）"
