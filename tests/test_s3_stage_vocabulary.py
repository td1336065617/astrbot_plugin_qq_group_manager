"""S5 护栏：stage 词表必须与文档/口径常量同源。

背景：stage 是统计口径的骨架（real_stages vs 降级）。写错一个字母（judeged）不会报错，
只会让事件永远落在「没审成」桶里、指标静默失真。这里用 AST 采集代码里**所有** stage 字面量，
与 REAL_STAGES + 文档名单比对。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.audit import REAL_STAGES

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DOCS = Path(__file__).resolve().parents[2] / "docs"


def _find_doc(*keywords: str) -> Path:
    """按关键词在 docs/ 里找文档（NFC 归一化比对，避免文件名归一化差异导致找不到）。"""
    import unicodedata


    if not DOCS.is_dir():
        pytest.skip(f"文档目录不在仓库内，跳过：{DOCS}")
    wanted = [unicodedata.normalize("NFC", k) for k in keywords]
    for candidate in sorted(DOCS.glob("*.md")):
        name = unicodedata.normalize("NFC", candidate.name)
        if all(k in name for k in wanted):
            return candidate
    pytest.skip(f"未找到文档（关键词={keywords}），跳过")

def _read_doc(path: Path) -> str:
    """读文档：文档不在仓库内（CI 只签出插件、改名目录回归）时跳过而不是失败。"""
    if not path.exists():
        pytest.skip(f"文档不在仓库内，跳过：{path}")
    return path.read_text(encoding="utf-8")

#: 降级（没审成）的全部 stage：与实现文档「口径备忘」、验收手册附录 C 必须一致
DEGRADED_STAGES = (
    "budget",
    "sampled_out",
    "throttled",
    "api_error",
    "circuit_open",
    "no_provider",
    "parse_error",
    "low_confidence",
    "illegal_value",
)


def _stage_literals(node: ast.AST) -> set[str]:
    """递归取表达式里的字符串字面量（兼容三元表达式）。"""
    found = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value)
    return found


def collected_stages() -> set[str]:
    """代码里所有 stage=... 关键字实参的字面量取值。"""
    stages: set[str] = set()
    files = [PLUGIN_ROOT / "main.py", *sorted((PLUGIN_ROOT / "src").glob("*.py"))]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "stage":
                        stages |= _stage_literals(keyword.value)
    return stages


def test_every_stage_literal_is_documented():
    written = collected_stages()
    assert written, "没采集到任何 stage 字面量（AST 采集失效？）"
    allowed = set(REAL_STAGES) | set(DEGRADED_STAGES)
    unknown = sorted(written - allowed)
    assert not unknown, f"这些 stage 未登记（会静默落进降级桶）：{unknown}"


def test_real_and_degraded_are_disjoint():
    overlap = set(REAL_STAGES) & set(DEGRADED_STAGES)
    assert not overlap, f"同一个 stage 既是真判定又是降级：{sorted(overlap)}"


def test_docs_and_code_agree_on_stage_vocabulary():
    """实现文档的口径备忘必须列全同样两组 stage（文档与代码同源）。"""
    doc = _read_doc(_find_doc("审核优化"))
    legend_start = doc.find("口径备忘（与代码同源")
    assert legend_start >= 0, "实现文档缺少「口径备忘」段"
    legend = doc[legend_start : legend_start + 900]
    missing_real = [name for name in REAL_STAGES if name not in legend]
    missing_degraded = [name for name in DEGRADED_STAGES if name not in legend]
    assert not missing_real, f"口径备忘漏了真判定 stage：{missing_real}"
    assert not missing_degraded, f"口径备忘漏了降级 stage：{missing_degraded}"


def test_manual_appendix_lists_degraded_bucket():
    manual = _read_doc(_find_doc("验收操作手册"))
    assert "judged" in manual and "degraded" in manual, "验收手册附录 C 缺少 stage 口径 SQL"
