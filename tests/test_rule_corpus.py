"""判定规则回归语料测试：正样本必须被送审，负样本不应被送审。

语料在 tests/data/rule_corpus.json，可用真机反馈持续补充。验收线（见
docs/判定规则优化方案.md §9）：正样本送审率 >= 95%，负样本误送审率 <= 5%。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.moderator import LLMModerator
from src.rules import RuleEngine

CORPUS_DIR = Path(__file__).resolve().parent / "data"
#: 合成语料（覆盖各类变体写法）+ 真实反垃圾语料（用户提供，已去敏）
CORPUS_FILES = ("rule_corpus.json", "rule_corpus_real.json")


def load_corpus(name: str = "rule_corpus.json") -> dict:
    return json.loads((CORPUS_DIR / name).read_text(encoding="utf-8"))


def build() -> tuple[RuleEngine, LLMModerator]:
    engine = RuleEngine(
        {
            "hard": [
                {
                    "id": "加群领资料",
                    "type": "literal",
                    "pattern": "加群领资料",
                    "action": ["warn", "recall", "mute"],
                    "scope": "all",
                    "enabled": True,
                }
            ],
            "soft": [],
        }
    )

    async def provider_call(request, system_prompt, user_prompt):  # pragma: no cover
        del request, system_prompt, user_prompt
        return '{"verdict":"allow","severity":1,"confidence":0.9}'

    settings = {
        "send_conditions": ["rule_hit", "risk>=60"],
        "llm_min_confidence": 0.7,
    }
    moderator = LLMModerator(provider_call, settings_getter=lambda: settings)
    return engine, moderator


def will_send(engine: RuleEngine, moderator: LLMModerator, text: str) -> bool:
    evaluation = engine.evaluate(text, group_id="g1")
    return bool(
        moderator.should_send(
            rule_summary=evaluation.rule_summary(),
            has_link=evaluation.has_link,
            long_text=evaluation.long_text,
            new_member=False,
            flood=evaluation.flood,
            recent=evaluation.recent_messages,
            risk_score=evaluation.score,
            has_contact=evaluation.has_contact,
            ad_template=bool(evaluation.template_hits),
        )
    )


def test_positive_corpus_send_rate():
    engine, moderator = build()
    failures: list[str] = []
    for name in CORPUS_FILES:
        corpus = load_corpus(name)
        missed = [text for text in corpus["positive"] if not will_send(engine, moderator, text)]
        rate = 1 - len(missed) / max(1, len(corpus["positive"]))
        if rate < corpus["targets"]["positive_send_rate"]:
            failures.append(name + "：" + f"{rate:.2%}" + " 低于目标，漏检 " + repr(missed))
    assert not failures, "；".join(failures)


def test_negative_corpus_false_positive_rate():
    engine, moderator = build()
    failures: list[str] = []
    for name in CORPUS_FILES:
        corpus = load_corpus(name)
        flagged = [text for text in corpus["negative"] if will_send(engine, moderator, text)]
        rate = len(flagged) / max(1, len(corpus["negative"]))
        if rate > corpus["targets"]["negative_send_rate"]:
            failures.append(name + "：" + f"{rate:.2%}" + " 超目标，误报 " + repr(flagged))
    assert not failures, "；".join(failures)


def test_exact_rule_still_enforces_directly():
    """精确命中的硬规则必须保持"直接处置"，不能被归一化改动影响。"""
    engine, _moderator = build()
    evaluation = engine.evaluate("加群领资料", group_id="g1")
    assert evaluation.enforce_actions == ["warn", "recall", "mute"]
    assert evaluation.score == 100


def test_variant_hit_does_not_auto_enforce():
    """形近字变体命中只作为送审依据（决策：交给 LLM 判定后再处置）。"""
    engine, moderator = build()
    text = "珈裙苓资料123456"
    evaluation = engine.evaluate(text, group_id="g1")
    assert evaluation.enforce_actions == []
    assert evaluation.hits, "变体应当命中规则或模板"
    assert evaluation.score >= 60
    assert will_send(engine, moderator, text) is True


def test_normalize_disabled_falls_back_to_old_behaviour():
    """关闭归一化后，变体应当回到"不命中"（可一键回退到旧行为）。"""
    engine = RuleEngine(
        {
            "hard": [
                {
                    "id": "kw",
                    "type": "literal",
                    "pattern": "加群领资料",
                    "action": ["recall"],
                    "scope": "all",
                    "enabled": True,
                }
            ],
            "soft": [],
        },
        templates=[],
    )
    # templates=[] 表示不使用模板；literal 规则对变体仍会走归一化兜底，
    # 这里验证的是"模板关闭后仅靠规则"的场景
    evaluation = engine.evaluate("珈裙苓资料123456", group_id="g1")
    assert evaluation.template_hits == [], "显式传空列表应彻底关闭模板规则"
    assert evaluation.enforce_actions == []
    # 变体仍靠 literal 规则的归一化兜底命中（只送审、不直接处置）
    assert evaluation.hits and evaluation.score >= 60


def test_fullwidth_latin_folds_into_hanzi():
    """全角字母经 NFKC 后仍是拉丁，必须再映射回汉字，否则骨架看不见"加群"。"""
    from src.normalize import normalize

    view = normalize("ｊｉａ群领资料")
    assert view.compact == "jia群领资料", "NFKC 负责全角→半角"
    assert view.skeleton == "加群领资料", "拉丁拼音别名负责 jia→加"
    engine, moderator = build()
    assert will_send(engine, moderator, "ｊｉａ群领资料") is True


def test_latin_alias_respects_word_boundary():
    """别名折叠必须按词边界，不能把 wxid_abc123 这类标识符拆坏。"""
    from src.normalize import normalize

    assert normalize("wxid_abc123").skeleton == "wxidabc123"
    assert normalize("jia qun").skeleton == "加群"


def test_kouqun_variants_count_as_channel():
    """叩/抠 折叠成扣后，"扣群"必须算真实渠道，否则 require_channel 的模板全不生效。"""
    engine, moderator = build()
    for text in ("扣群领资料", "叩群领资料 秒通过", "抠群领资料"):
        evaluation = engine.evaluate(text, group_id="g1")
        assert evaluation.template_hits, text
        assert will_send(engine, moderator, text) is True, text


def test_soft_open_group_lure_is_sent_but_not_enforced():
    """无外链的软性开群话术：场景 + 诱饵 + 催促 三件套齐备才送审，且不直接处置。"""
    engine, moderator = build()
    text = "新群开张，福利多多，手慢无"
    evaluation = engine.evaluate(text, group_id="g1")
    assert [hit.rule_id for hit in evaluation.template_hits] == ["ad_open"]
    assert evaluation.enforce_actions == []
    assert will_send(engine, moderator, text) is True


def test_weak_scene_words_do_not_trigger_open_template():
    """弱场景词（本群/群内）与只有场景词的消息都不该被送审。"""
    engine, moderator = build()
    for text in (
        "本群资料限时开放，抓紧",
        "群内公告：本周比赛时间改到周日",
        "我建了个新群，大家来玩",
        "新群开张，福利多多",
    ):
        assert not will_send(engine, moderator, text), text


def test_pinyin_matching_when_available():
    """同音匹配：安装了 pypinyin 时，拼音规则应能命中同音变体。"""
    from src.normalize import pinyin_available

    if not pinyin_available():
        import pytest

        pytest.skip("未安装 pypinyin（可选依赖），跳过同音匹配用例")
    engine = RuleEngine(
        {
            "hard": [
                {
                    "id": "pinyin-rule",
                    "type": "pinyin",
                    "pattern": "加群领资料",
                    "action": ["warn"],
                    "scope": "all",
                    "enabled": True,
                }
            ],
            "soft": [],
        },
        templates=[],
        pinyin_enabled=True,
    )
    evaluation = engine.evaluate("jiaqun ling ziliao", group_id="g1")
    assert any(hit.rule_type == "pinyin" for hit in evaluation.hits)


def test_template_hit_sends_but_does_not_enforce():
    """模板命中只送审（决策：违规后由 LLM 判定结果决定处置）。"""
    engine = RuleEngine({"hard": [], "soft": []})
    evaluation = engine.evaluate("进群领取资料", group_id="g1")
    assert evaluation.template_hits
    assert evaluation.enforce_actions == []
    assert evaluation.score >= 60
