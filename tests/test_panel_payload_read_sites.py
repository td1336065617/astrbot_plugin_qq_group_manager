"""B3（P1-e）通用护栏：面板能编辑的每个设置键，后端必须有人读。

没有这道护栏就会不断出现「面板能改、后端从不读」的死键 —— 历史上抓到的就有
`risk_send_threshold`、`auto_blacklist`、`normalize_enabled` 三个。
"""

from __future__ import annotations

import pathlib

from tests.test_s3_panel_payload import _main_panel_payload_keys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_FILES = [*sorted((ROOT / "src").glob("*.py")), ROOT / "main.py"]

#: 读取点的四种真实写法；只认第一种会误伤 sample_rate / llm_* / appeal_* 等合法键
READ_PATTERNS = (
    'settings.get("{key}"',
    "settings.get('{key}'",
    'settings["{key}"]',
    "settings['{key}']",
    'settings().get("{key}"',
    "settings().get('{key}'",
    'get_setting("{key}"',
    "get_setting('{key}'",
)


def _read_sites(key: str) -> list[str]:
    """只认真正的代码行：注释/docstring 里出现 settings.get("key" 不算读取点。"""
    hits: list[str] = []
    for path in SRC_FILES:
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        text = "\n".join(lines)
        if any(pattern.format(key=key) in text for pattern in READ_PATTERNS):
            hits.append(path.name)
    return hits


def test_every_panel_editable_key_is_read_by_backend():
    keys = sorted(_main_panel_payload_keys())
    assert keys, "没能从面板 payload 抽出键集合（契约测试的提取器改了？）"
    assert len(keys) >= 45, f"抽出的键太少（{len(keys)} 个）：提取器可能退化了，护栏会静默失效"
    missing = [key for key in keys if not _read_sites(key)]
    assert not missing, "面板能改但后端从不读取的键（死键）：" + "、".join(missing)
