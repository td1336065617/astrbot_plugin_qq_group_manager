"""R3-2：模型输出只漏一个开引号时的保守修复（生产实测 4/4 条 parse_error 都是这种毛病）。

约束：只修 analysis / evidence / reason / category 四个已知键的「漏开引号」；
既不能碰正常 JSON，也不能把"正文复述"这种非判定输出修成判定。
两种真实形状都要覆盖：① 键在中间（值后有分隔逗号）；② 键是最后一个字段（值后无逗号，且前一个字段有逗号）。
"""

from src.moderator import _REPAIR_STATS, extract_json_object, parse_verdict, repair_json_text

KEYS = ("analysis", "evidence", "reason", "category")

BODY = [
    '  "analysis": "只是情绪表达",',
    '  "evidence": "群里常见互动",',
    '  "verdict": "violation",',
    '  "category": "辱骂攻击",',
    '  "severity": 3,',
    '  "confidence": 0.9,',
    '  "reason": "针对特定成员",',
    '  "suggested_action": "mute_and_recall"',
]


def _shaped(key: str, value: str, as_last: bool = False) -> str:
    """造一段「其余字段都合规、只有 key 漏了开引号」的模型输出。"""
    lines = []
    inserted = False
    for line in BODY:
        if line.strip().startswith('"' + key + '"'):
            if as_last:
                continue  # 稍后作为最后一个字段补进去
            lines.append('  "' + key + '": ' + value + '",')
            inserted = True
            continue
        lines.append(line)
    if as_last:
        if lines and not lines[-1].rstrip().endswith(","):
            lines[-1] = lines[-1].rstrip() + ","
        lines.append('  "' + key + '": ' + value + '"')
        inserted = True
    assert inserted, (key, "造样失败")
    return "\n".join(["{" ] + lines + ["}"])


def test_repair_fixes_each_known_key():
    for key in KEYS:
        for as_last in (False, True):
            raw = _shaped(key, "这里漏了开引号", as_last=as_last)
            assert extract_json_object(raw) is None, (key, as_last, "本该解析失败")
            fixed = repair_json_text(raw)
            assert fixed != raw, (key, as_last, "没被修复")
            payload = extract_json_object(fixed)
            assert payload is not None, (key, as_last, "修复后仍无法解析", fixed)
            assert payload[key] == "这里漏了开引号", (key, as_last, payload.get(key))
            assert payload["verdict"] == "violation", (key, as_last, "其它字段被改动了")


def test_repaired_output_is_a_real_judgement():
    before = _REPAIR_STATS.get("n", 0)
    verdict = parse_verdict(_shaped("reason", "只是漏了个引号"))
    assert verdict.parse_error is False
    assert verdict.stage == "judged"
    assert verdict.verdict == "violation"
    assert verdict.severity == 3
    assert verdict.reason == "只是漏了个引号"
    assert _REPAIR_STATS.get("n", 0) == before + 1


def test_repair_leaves_valid_json_untouched():
    raw = '{"verdict": "allow", "confidence": 0.98, "reason": "正常"}'
    assert repair_json_text(raw) == raw


def test_repair_leaves_non_json_untouched():
    raw = "这是模型的解释性文字，根本不是 JSON。"
    assert repair_json_text(raw) == raw


def test_repair_skips_values_that_already_contain_quotes():
    line = '  "reason": 正文里塞了 " 引号 的复述,",'
    raw = "{\n" + line + "\n}"
    assert repair_json_text(raw) == raw


def test_repair_cannot_manufacture_a_verdict():
    """只有 reason、没有 verdict 的输出，修完也不能变成判定。"""
    raw = '{\n  "reason": 复述了用户的正文,\n}'
    verdict = parse_verdict(raw)
    assert verdict.parse_error is True
    assert verdict.stage == "parse_error"
    assert verdict.verdict == "review"
