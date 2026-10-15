"""绕过手法覆盖测试（依据公开研究：6 类高级文本绕过手法）。

- 手法一 拼音替换（du bo / seqing）
- 手法二 谐音字（已在 homoglyph 表中覆盖）
- 手法三 拆字/偏旁（君羊/力口/贝者）
- 手法四 符号干扰（加-群 / ➕v）
- 手法五 emoji 编码（🔞🍑🎰🃏）
- 手法六 中英混排/leet（色q1ng / 赌b0）
"""
from __future__ import annotations

import pytest

from src import rules

# 应命中：每类手法的真实广告写法
POSITIVE = [
    ("emoji+诱饵", "福利资源 🔞🍑 懂的都懂"),
    ("emoji+渠道", "新片合集 🎰🃏 加v领取"),
    ("赌博黑话+渠道", "菠菜盘新用户送彩金，进群找代理拿返水"),
    ("赌博黑话+交易", "跑分平台招代理，日结佣金，加v详聊"),
    ("接码养号", "接码 卡商 料商 四件套 出货，私聊谈价"),
    ("放贷诈骗", "无抵押秒下款，黑户可下，加v咨询放款"),
    ("中英混排-色", "色q1ng资源 加v领取"),
    ("中英混排-赌", "赌b0平台 进群玩"),
    ("拼音写法", "dubo 平台 进群 加v"),
    ("拆字-加", "力口群领资料"),
    ("拆字-赌", "贝者博平台 加v"),
    ("符号-加号", "资料 ➕v shhdjdkl"),
]

# 不应误报：这些词在正常语境里很常见（蔬菜/考研/性能测试/讨论）
NEGATIVE = [
    "今天中午吃菠菜炒鸡蛋",
    "我考研上岸了！",
    "跑分测试结果出来了，C++ 比 Python 快",
    "这个 emoji 🍑 好可爱",
    "买了个新键盘，💰花了不少",
    "接码平台是干什么的？有人在课上问",
    "四件套指的是什么？课程里讲过",
    "C++ 和 Python 哪个好写",
    "力口是什么字？我在学拆字",
    "贝者是什么字",
    "今天去水房打水",
    "菜农种的菜真新鲜",
]


@pytest.fixture()
def engine() -> rules.RuleEngine:
    return rules.RuleEngine()


@pytest.mark.parametrize("label,text", POSITIVE)
def test_evasion_techniques_are_caught(engine, label, text):
    evaluation = engine.evaluate(text=text, group_id="demo")
    assert evaluation.score >= 45 or evaluation.template_hits, (
        label,
        evaluation.score,
        [h.pattern for h in evaluation.hits],
        evaluation.signals,
    )


@pytest.mark.parametrize("text", NEGATIVE)
def test_black_slang_words_in_normal_context_are_not_flagged(engine, text):
    """菠菜/上岸/跑分/接码/水房/菜农 等词有正常含义，单独出现不得判违规。"""
    evaluation = engine.evaluate(text=text, group_id="demo")
    assert evaluation.score < 70, (text, evaluation.score)
    assert not evaluation.template_hits, (text, [h.pattern for h in evaluation.hits])


def test_leet_fold_view_visible():
    """色q1ng / 赌b0 折叠后应能被模板与信号看到。"""
    from src.normalize import pinyin_available

    if not pinyin_available():
        import pytest

        pytest.skip("未安装 pypinyin（可选依赖），跳过拼音视图用例")
    from src.normalize import leet_fold, normalize

    assert leet_fold("色q1ng") == "色qing"
    assert leet_fold("赌b0") == "赌bo"
    views = normalize("色q1ng资源", homoglyph=None, with_pinyin=True)
    assert views.leet == "色qing资源"
    # 拼音视图基于折叠后的文本，因此能算出 seqing
    assert "seqing" in views.pinyin


def test_emoji_code_needs_bait_or_channel(engine):
    """单独一个 emoji 不算违规，必须与诱饵/渠道词共现。"""
    alone = engine.evaluate(text="今天心情不错 🍑", group_id="demo")
    assert not alone.template_hits
    paired = engine.evaluate(text="福利资源 🍑 加v", group_id="demo")
    assert paired.template_hits or paired.score >= 45
