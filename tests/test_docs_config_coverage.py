"""文档守门：docs/CONFIG.md 必须覆盖每一个配置项、标注校对版本。

背景：用户反馈「有的文档落后几个版本」。这里用测试把「文档跟着代码走」变成硬要求，
而不是靠人记得手动同步。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PLUGIN_ROOT / "docs" / "CONFIG.md"


def _default_keys() -> list[str]:
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    models = importlib.import_module(f"{PLUGIN_ROOT.name}.src.models")
    return sorted(models.default_settings().keys())


def _plugin_version() -> str:
    text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*([0-9.]+)", text)
    assert match, "metadata.yaml 里没有 version"
    return match.group(1)


def test_every_setting_key_is_mentioned():
    text = CONFIG.read_text(encoding="utf-8")
    missing = [key for key in _default_keys() if key not in text]
    assert not missing, f"docs/CONFIG.md 未提及这些配置项（文档落后了）：{missing}"


def _speed_table() -> str:
    """速查表正文。注意必须从「## 附：」这一行切起——文件顶部的导语里也提到过表名，
    用 「附：完整配置项速查」 直接 index 会切到页首、等于没约束（这个坑踩过一次）。"""
    text = CONFIG.read_text(encoding="utf-8")
    marker = "## 附：完整配置项速查"
    assert marker in text, "速查表不见了"
    return text[text.index(marker) :]


def test_speed_table_lists_every_key():
    table = _speed_table()
    missing = [key for key in _default_keys() if f"| `{key}` |" not in table]
    assert not missing, f"速查表漏了这些键：{missing}"


def test_speed_table_rows_match_settings_exactly():
    """行数必须与配置项数一致：既防漏写，也防写了代码里没有的键。"""
    rows = re.findall(r"(?m)^\| `([a-z_0-9]+)` \|", _speed_table())
    assert sorted(rows) == _default_keys(), (
        f"速查表行数与配置项不一致：表 {len(rows)} 行 / 代码 {len(_default_keys())} 项"
    )


def test_doc_declares_current_version():
    """文档顶部必须写明校对到哪个版本；发版时忘了改这里会红。"""
    text = CONFIG.read_text(encoding="utf-8")
    assert f"校对版本：v{_plugin_version()}" in text, (
        f"docs/CONFIG.md 的校对版本与 metadata.yaml（{_plugin_version()}）不一致"
    )



def test_doc_index_declares_current_version():
    """文档索引的「校对基准」也必须跟版本走——它和 CONFIG 标头是两份文档，容易只改一份。"""
    index = (PLUGIN_ROOT / "docs" / "文档索引.md").read_text(encoding="utf-8")
    version = _plugin_version()
    assert f"校对基准：**v{version}" in index, (
        f"docs/文档索引.md 的校对基准不是当前版本 {version}"
    )
    assert "v0.15." in index, "索引里没有版本行"


def test_readme_declares_current_version():
    """README 顶部的「文档校对」标头也必须跟版本走 —— 它曾停在 v0.15.16 好几版（守门只查 CONFIG 与索引时漏掉）。"""
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"文档校对：v{_plugin_version()}" in readme, (
        f"README 的文档校对标头不是当前版本 {_plugin_version()}"
    )
