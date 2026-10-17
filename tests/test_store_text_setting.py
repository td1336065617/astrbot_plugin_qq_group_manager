"""store_text 接线（0.15.11）：true 时存完整正文（上限 4000 字），false 时只存 120 字摘要。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.actions import excerpt_for_store


class _Store:
    def __init__(self, value):
        self._value = value

    def get_setting(self, key, default=None):
        return self._value if key == "store_text" else default


def test_default_keeps_excerpt_only():
    text = "加" * 500
    got = excerpt_for_store(_Store(False), text)
    assert len(got) <= 120, got


def test_enabled_stores_full_text():
    text = "加" * 500
    got = excerpt_for_store(_Store(True), text)
    assert got == text


def test_enabled_still_caps_at_4000():
    text = "加" * 5000
    got = excerpt_for_store(_Store(True), text)
    assert len(got) == 4000


def test_missing_store_or_broken_setting_falls_back_to_excerpt():
    class _Boom:
        def get_setting(self, key, default=None):
            raise RuntimeError("boom")

    text = "加" * 300
    assert len(excerpt_for_store(None, text)) <= 120
    assert len(excerpt_for_store(_Boom(), text)) <= 120
