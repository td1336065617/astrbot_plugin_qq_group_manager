"""B3（P1-e）通用护栏：面板能编辑的每个设置键，后端必须有人读。

没有这道护栏就会不断出现「面板能改、后端从不读」的死键 —— 历史上抓到的就有
`risk_send_threshold`、`auto_blacklist`、`normalize_enabled` 三个。
"""

from __future__ import annotations

import pathlib
import re

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
    'effective.get("{key}"',
    "effective.get('{key}'",
)

#: 少量键的读取点写法确实不在上面的模式里；豁免必须带上 file:line 证据，
#: 否则这道护栏会退化成"什么都放过"。
EVIDENCE_EXEMPTIONS = {
    "join_llm_fallback_provider_ids": "main.py:750/758 用动态键 get_setting(provider_setting/fallback_key) 读取；src/store.py:178 归一化列表",
}


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


def _join_field_spec_keys() -> set[str]:
    """从面板 app.js 的 JOIN_FIELD_SPECS 抽出「入群审批页」可编辑的键。"""
    text = (ROOT / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    start = text.index("const JOIN_FIELD_SPECS = {")
    end = text.index(chr(10) + "};", start)
    return set(re.findall(r"^  ([a-z_][a-z0-9_]*): [{]", text[start:end], flags=re.M))


def test_join_spec_keys_are_read_by_backend():
    """护栏扩展（审计建议）：入群审批页能编辑的键，后端也必须有真实读取点。"""
    keys = sorted(_join_field_spec_keys())
    assert len(keys) >= 20, f"抽出的入群键太少（{len(keys)} 个）：提取器可能退化了"
    missing = [key for key in keys if not _read_sites(key) and key not in EVIDENCE_EXEMPTIONS]
    assert not missing, f"入群页能编辑但后端从不读的键：{missing}"


#: 豁免必须附带"证据仍存在"的校验：那处读取点被删掉时这条断言会红，
#: 避免出现「代码早改了、豁免还留着」的静默失效。
EVIDENCE_CHECKS = {
    "join_llm_fallback_provider_ids": ("main.py", "fallback_key"),
}


def test_evidence_exemptions_still_point_at_real_read_sites():
    for key, (rel, snippet) in EVIDENCE_CHECKS.items():
        assert key in EVIDENCE_EXEMPTIONS, f"{key} 有证据校验却不在豁免表里"
        path = ROOT / rel
        assert path.exists(), f"豁免 {key} 指向的文件不存在：{rel}"
        assert snippet in path.read_text(encoding="utf-8"), (
            f"豁免 {key} 的证据代码已不存在（{rel} 中找不到 {snippet!r}）：豁免该撤销"
        )
