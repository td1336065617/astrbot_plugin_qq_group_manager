"""面板校验必须真的被 config_set 调用（防"函数在、接线掉了"）。"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_config_set_calls_validate_before_update() -> None:
    source = (ROOT / "src" / "web_api.py").read_text(encoding="utf-8")
    call = "validate_boundary_patch(data)"
    assert call in source, "config_set 必须调用 validate_boundary_patch"
    assert source.index(call) < source.index("await store.update_settings(data)"), (
        "校验必须在写入之前"
    )


def test_panel_categories_match_backend() -> None:
    from src.models import CATEGORIES

    js = (ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    start = js.index("const BOUNDARY_CATS = [")
    line = js[start : js.index("];", start) + 1]
    names = [chunk.strip().strip("'").strip('"') for chunk in line.split("[")[-1].split("]")[0].split(",")]
    names = [name for name in names if name]
    assert set(names) == set(CATEGORIES), f"前端类别与后端不一致：{sorted(set(names) ^ set(CATEGORIES))}"
