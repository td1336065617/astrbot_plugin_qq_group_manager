"""ai_common 自测（纯逻辑，不依赖 astrbot）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_common as ac


def test_ok_shape() -> None:
    payload = json.loads(ac.ok({"a": 1}))
    assert payload == {"ok": True, "data": {"a": 1}}


def test_fail_shape_and_codes() -> None:
    payload = json.loads(ac.fail("permission_denied"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "permission_denied"
    assert payload["error"]["message"]
    raw = json.loads(ac.fail("不存在的码", ""))
    assert raw["error"]["code"] == "internal_error"


def test_fail_drops_empty_extras() -> None:
    payload = json.loads(ac.fail("throttled", "慢一点", retry_after=0, hint=""))
    assert payload["error"]["retry_after"] == 0
    assert "hint" not in payload["error"]


def test_to_bool_accepts_strings() -> None:
    assert ac.to_bool("false") is False
    assert ac.to_bool("0") is False
    assert ac.to_bool("TRUE") is True
    assert ac.to_bool(None, default=False) is False
    assert ac.to_bool("乱写", default=True) is True


def test_clamp_and_enum() -> None:
    assert ac.clamp_int("600", 1, 43200) == 600
    assert ac.clamp_int("999999", 1, 43200) == 43200
    assert ac.clamp_int("abc", 1, 43200) is None
    assert ac.clamp_int("abc", 1, 43200, default=5) == 5
    assert ac.pick_enum(" FULL ", ["summary", "full"]) == "full"
    assert ac.pick_enum("乱写", ["summary", "full"]) is None


def test_mask_id_hides_middle() -> None:
    assert ac.mask_id("ABCDEFGHIJKL") == "ABCD****IJKL"
    assert ac.mask_id("123") == "***"
    assert ac.mask_id("") == ""


def test_sanitize_strips_control_and_truncates() -> None:
    assert ac.sanitize("a\x00b\n\n\n\nc", 10) == "ab\n\nc"
    assert len(ac.sanitize("x" * 100, 10)) == 10


def test_throttle_blocks_after_limit() -> None:
    throttle = ac.ToolThrottle(limit=2, window=60)
    assert throttle.check("t", "u", now=100) == 0
    assert throttle.check("t", "u", now=101) == 0
    wait = throttle.check("t", "u", now=102)
    assert wait > 0
    assert throttle.check("t", "u", now=200) == 0


def test_throttle_counts_failures() -> None:
    throttle = ac.ToolThrottle(limit=2, window=60)
    assert throttle.note_failure("t", "u", now=1) == 0
    assert throttle.note_failure("t", "u", now=2) > 0
    assert throttle.failures("t", "u") == 2
    throttle.reset()
    assert throttle.failures("t", "u") == 0


def test_throttle_memory_bounded() -> None:
    throttle = ac.ToolThrottle(limit=5, window=60, max_keys=16)
    for index in range(200):
        throttle.check("t", f"key{index}", now=1000 + index)
    assert len(throttle._hits) <= 16


def test_join_lines_limit() -> None:
    text = ac.join_lines([f"row{i}" for i in range(5)], limit=2)
    assert text.startswith("row0\nrow1")
    assert "已省略 3 条" in text
