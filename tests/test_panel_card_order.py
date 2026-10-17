"""策略页卡片顺序契约：按「使用频率 + 因果链」排序（用户反馈「运行参数在中间」后确定的结论）。

顺序是产品决定，这里只把它钉住，避免以后被无意挪回去。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "pages" / "manage" / "app.js"

#: 从上到下：每天要动的 → 谁来判 → 怎么处置 → 提示词 / 工具 → 进阶微调 → 存档与验证
EXPECTED = [
    "运行参数",
    "模型",
    "处置矩阵",
    "提示词",
    "AI 能力（工具）",
    "规则增强（变体识别 / 风险分）",
    "竞赛域名白名单",
    "申诉闭环",
    "保存",
    "试跑（dry-run）",
]


def _card_titles(view: str) -> list[str]:
    """按源码顺序抽出某视图里 root.appendChild(card('标题', ...)) 的标题。"""
    text = APP.read_text(encoding="utf-8")
    start = text.index(f"async function {view}(root) {{")
    body = text[start : text.index(chr(10) + "}", start)]
    titles: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        prefix = "root.appendChild(card("
        if not stripped.startswith(prefix):
            continue
        rest = stripped[len(prefix) :].lstrip()
        quote = rest[:1]
        if quote in ("'", chr(34)):
            titles.append(rest[1 : rest.index(quote, 1)])
    return titles


def test_policy_cards_are_ordered_by_usage_frequency():
    titles = _card_titles("viewPolicy")
    assert titles == EXPECTED, f"策略页卡片顺序变了：{titles}"


def test_advanced_cards_come_after_daily_ones():
    """"运行参数"（每天要动）必须在前三张里；进阶微调必须排在后面。"""
    titles = _card_titles("viewPolicy")
    assert "运行参数" in titles[:3], f"运行参数不该被埋到中间：{titles}"
    for advanced in ("规则增强（变体识别 / 风险分）", "竞赛域名白名单", "申诉闭环"):
        assert titles.index(advanced) > titles.index("处置矩阵"), f"{advanced} 应沉底：{titles}"

