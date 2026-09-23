"""文本归一化：让规则层"看得见"形近字、插符号、全角等规避写法。

三个视图（规则会同时在这些视图上求值）：
- raw：原样，保持与旧版行为一致；
- compact：NFKC（全角转半角/兼容字符归一）→ 小写 → 去零宽与变体选择符 → 去空白与干扰符；
- skeleton：在 compact 基础上做形近字/同音字映射，再去掉非中日韩/字母/数字字符。

可选第四个视图：
- pinyin：环境已安装 pypinyin 时给出拼音骨架（jiaqunlingziliao），未安装则为空串，
  因此插件保持"零第三方依赖"（见 docs/判定规则优化方案.md §5.1）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .simplify import to_simplified

#: 内置形近字/同音字基线（可在 WebUI「形近字表」中增删覆盖）
BUILTIN_HOMOGLYPH: dict[str, str] = {
    "珈": "加",
    "伽": "加",
    "茄": "加",
    "枷": "加",
    "佳": "加",
    "家": "加",
    "架": "加",
    "裙": "群",
    "羣": "群",
    "麇": "群",
    "苓": "领",
    "伶": "领",
    "玲": "领",
    "岭": "领",
    "呤": "领",
    "領": "领",
    "進": "进",
    "逬": "进",
    "咨": "资",
    "姿": "资",
    "滋": "资",
    "兹": "资",
    "資": "资",
    "廖": "料",
    "疗": "料",
    "聊": "料",
    "薇": "微",
    "巍": "微",
    "溦": "微",
    "芯": "信",
    "薪": "信",
    "馨": "信",
    "抠": "扣",
    "蔻": "扣",
    "釦": "扣",
    "爿": "片",
    "叩": "扣",
    "君": "群",
    "浮": "福",
    # 网络异体字 / 仿造字（OpenCC 繁简表不覆盖，实测用于规避过滤）
    # 例："侽 亾 哋 𝕤𝕖𝕔𝕣𝕖𝕥" "讓 沵 變 佷 侽 亾" "純 兲 嘫" "芣 滿 噫"
    "侽": "男",
    "亾": "人",
    "亼": "人",
    "哋": "的",
    "嘚": "的",
    "旳": "的",
    "沵": "你",
    "伱": "你",
    "妳": "你",
    "袮": "你",
    "佷": "很",
    "媺": "美",
    "兲": "天",
    "迗": "天",
    "嘫": "然",
    "莋": "作",
    "咗": "作",
    "鼡": "用",
    "甪": "用",
    "吢": "心",
    "芣": "不",
    "丆": "不",
    "噫": "意",
    "佲": "名",
    "苝": "北",
    "炷": "注",
    "嫃": "真",
    "盆": "朋",
    "伖": "友",
    "玖": "久",
    "乆": "久",
    "萣": "定",
    "珂": "可",
    "収": "收",
    "岀": "出",
    "甴": "由",
    "沩": "为",
    "潙": "为",
    "籹": "女",
    "囡": "女",
    "莪": "我",
    "涐": "我",
    "溡": "时",
    "圏": "圈",
    "咔": "卡",
    "悝": "理",
    "萪": "科",
    "対": "对",
    "哯": "现",
    "巳": "已",
    "浉": "师",
    "溮": "师",
    "㳡": "过",
    "孖": "子",
    "ㄧ": "一",
    "仴": "月",
    "杺": "心",
    "諴": "诚",
    "噺": "新",
    "囍": "喜",
    "冄": "再",
    "曱": "甲",
    "甶": "由",
    "甼": "町",
    "莉": "利",
    "撩": "聊",
    "片孑": "片",
    "機": "机",
    "發": "发",
    "車": "车",
    "點": "点",
    "擊": "击",
    "鏈": "链",
    "結": "结",
    "圖": "图",
    "視": "视",
    "頻": "频",
    "費": "费",
    "價": "价",
    "買": "买",
    "賣": "卖",
    "錢": "钱",
    "賺": "赚",
    "開": "开",
    "關": "关",
    "門": "门",
    "們": "们",
    "個": "个",
    "這": "这",
    "邊": "边",
    "號": "号",
    "碼": "码",
    "網": "网",
    "頁": "页",
    "檔": "档",
    "軟": "软",
    "體": "体",
    "選": "选",
    "讓": "让",
    "說": "说",
    "實": "实",
    "無": "无",
    "萬": "万",
    "優": "优",
    "歡": "欢",
    "樂": "乐",
    "夠": "够",
    "還": "还",
    "過": "过",
    "時": "时",
    "現": "现",
    "應": "应",
    "該": "该",
    "與": "与",
    "爲": "为",
    "為": "为",
    "從": "从",
    "會": "会",
    "後": "后",
    "發車": "发车",
    "惠": "惠",
    "兼职": "兼职",
    "刷單": "刷单",
    "顔": "颜",
    "視頻": "视频",
    "址": "址",
    "接": "接",
    "免費": "免费",
    "領取": "领取",
}

#: 拆字/多字混淆短语（在骨架化之前替换）
MULTI_PHRASES: dict[str, str] = {
    "君羊": "群",
    "看爿": "看片",
    "白勺": "的",
    "亻尔": "你",
    "辶文": "这",
    "扌立": "拉",
    "弓虽": "强",
    "口我": "哦",
    "月永": "朋",
    "票风": "飘",
    # 拆字组合（研究：广告/赌博内容常用偏旁拆分绕过滤）
    "力口": "加",
    "贝者": "赌",
    "木木": "林",
    "口贝": "呗",
    "禾中": "种",
}

#: 拉丁拼音/缩写别名（在骨架化之前按词边界折叠）。
#: 全角字母经 NFKC 后是纯拉丁（`ｊｉａ群领资料` → `jia群领资料`），
#: 若不映射回汉字，骨架视图就看不见"加群"（实测漏检）。
LATIN_ALIAS: dict[str, str] = {
    "jiaqun": "加群",
    "kouqun": "扣群",
    "jinjun": "进群",
    "ziliao": "资料",
    "weixin": "微信",
    "fuli": "福利",
    "ling": "领",
    "jia": "加",
    "qun": "群",
    "kou": "扣",
    "vx": "微信",
    "wx": "微信",
}

#: 词边界：两侧都不能是字母/数字，避免 wxid_abc 这类标识符被拆坏
_LATIN_ALIAS_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(sorted(LATIN_ALIAS, key=len, reverse=True)) + r")(?![a-z0-9])"
)

#: 干扰符号（compact 视图会去掉）：空白、常见标点、装饰符号
_INTERFERENCE = set(
    " \t\r\n\v\f.,-_~^*|/\\+=!?;:\"'()[]{}<>@#$%&",
)

#: 全角标点（NFKC 之后基本已转半角，这里兜底）
_INTERFERENCE.update("！？，。：；、～…—－＿　·•●○◆◇■□★☆→←↑↓")

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00A0, *range(0xFE00, 0xFE10)]
)

_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff"
    "\U00002190-\U000021ff\U00002b00-\U00002bff\U0000fe0f\U000020e3]"
)

_WORD_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+")

#: 中英混排/leet 折叠（研究：手法六）：把数字/符号还原成字母，
#: 使 "色q1ng"→"色qing"、"赌b0"→"赌bo"，从而能被拼音/关键词视图命中。
_LEET_FOLD = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "6": "g",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "l",
    }
)


def leet_fold(text: str) -> str:
    """把 leet/数字替代还原为字母（仅用于匹配，不改动原文）。"""
    return (text or "").translate(_LEET_FOLD)

#: 符号形式的"加"：必须在去噪**之前**映射成汉字。
#: 否则 ➕/✚ 会落进 _EMOJI_RE 区间被当表情删掉、+ 会被当干扰符删掉，
#: 规则层就完全看不到"加v/加微"（实测漏检：`资料 ➕v shhdjdkl`）。
_PLUS_TO_JIA = str.maketrans(
    {
        "➕": "加",  # U+2795 heavy plus（在 _EMOJI_RE 区间内）
        "✚": "加",  # U+271A heavy greek cross
        "＋": "加",  # 全角加号
        "﹢": "加",  # 小号加号
        "ᐩ": "加",  # 加拿大音节文字加号
        "+": "加",  # 半角加号
    }
)

#: 中文数字（用于识别用汉字写的号码）
CN_DIGITS = "零一二三四五六七八九〇两"

_pinyin_available: bool | None = None


def pinyin_available() -> bool:
    """环境是否安装了 pypinyin（可选依赖，未安装不影响功能）。"""
    global _pinyin_available
    if _pinyin_available is None:
        try:
            import pypinyin  # noqa: F401

            _pinyin_available = True
        except Exception:
            _pinyin_available = False
    return _pinyin_available


def to_pinyin_skeleton(text: str) -> str:
    """把文本转成"无分隔拼音骨架"（jiaqunlingziliao），失败返回空串。"""
    if not text or not pinyin_available():
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        # errors="default"：非汉字原样保留（否则 latin 写法会被整段丢弃）
        parts = lazy_pinyin(text, style=Style.NORMAL, errors="default")
    except Exception:  # pragma: no cover - pypinyin 异常时降级
        return ""
    return "".join(char for char in "".join(parts) if char.isalnum())


@dataclass(slots=True)
class NormalizedText:
    """一条消息的多个归一化视图。"""

    raw: str = ""
    compact: str = ""
    skeleton: str = ""
    pinyin: str = ""
    #: leet/中英混排折叠后的 compact（用于识别 色q1ng 这类写法）
    leet: str = ""
    homoglyph_hits: list[str] = field(default_factory=list)

    def views(self) -> list[str]:
        """去重后的全部视图（用于多视图匹配）。"""
        seen: list[str] = []
        for value in (self.raw, self.compact, self.skeleton, self.pinyin):
            if value and value not in seen:
                seen.append(value)
        return seen


def _strip_noise(text: str) -> str:
    value = text.translate(_ZERO_WIDTH)
    return _EMOJI_RE.sub("", value)


def compact_text(text: str, *, keep_case: bool = False) -> str:
    """compact 视图：NFKC + 去噪 + 去干扰符（默认转小写）。"""
    value = unicodedata.normalize(
        "NFKC", _strip_noise((text or "").translate(_PLUS_TO_JIA))
    )
    # 繁→简（OpenCC 离线生成的逐字映射）：广告常用繁体/异体字规避过滤，
    # 例："進媺國原裝偉哥讓沵變佷侽亾" → "进媺国原装伟哥让沵变佷侽亾"。
    value = to_simplified(value)
    if not keep_case:
        value = value.lower()
    return "".join(char for char in value if char not in _INTERFERENCE)


def skeleton_text(text: str, homoglyph: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """skeleton 视图：compact 之上做形近字映射，再去掉非中日韩/字母/数字字符。

    返回 (骨架文本, 命中的形近字列表)。
    """
    table = dict(BUILTIN_HOMOGLYPH)
    if homoglyph:
        table.update({str(k): str(v) for k, v in homoglyph.items() if str(k) and str(v)})
    compact = compact_text(text)
    for phrase, replacement in MULTI_PHRASES.items():
        if phrase in compact:
            compact = compact.replace(phrase, replacement)
    compact = _LATIN_ALIAS_RE.sub(lambda match: LATIN_ALIAS[match.group(1)], compact)
    hits: list[str] = []
    mapped: list[str] = []
    for char in compact:
        replacement = table.get(char)
        if replacement and replacement != char:
            hits.append(f"{char}->{replacement}")
            mapped.append(replacement)
        else:
            mapped.append(char)
    skeleton = "".join(mapped)
    skeleton = "".join(char for char in skeleton if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    # 去重但保持顺序
    uniq: list[str] = []
    for item in hits:
        if item not in uniq:
            uniq.append(item)
    return skeleton, uniq


def normalize(
    text: str, *, homoglyph: dict[str, str] | None = None, with_pinyin: bool = False
) -> NormalizedText:
    """生成全部视图。"""
    raw = text or ""
    compact = compact_text(raw)
    skeleton, hits = skeleton_text(raw, homoglyph)
    folded = leet_fold(compact)
    # 拼音视图基于折叠后的文本：这样 "色q1ng" 也能算出 seqing 命中拼音规则
    pinyin = to_pinyin_skeleton(folded) if with_pinyin else ""
    return NormalizedText(
        raw=raw,
        compact=compact,
        skeleton=skeleton,
        pinyin=pinyin,
        leet=folded,
        homoglyph_hits=hits,
    )


def digits_of(text: str) -> str:
    """取文本中的数字串（含中文数字转换），用于"长号码"类判定。"""
    value = compact_text(text)
    out: list[str] = []
    for char in value:
        if char.isdigit():
            out.append(char)
        elif char in CN_DIGITS:
            out.append(str(CN_DIGITS.index(char) % 10))
    return "".join(out)


def longest_digit_run(text: str) -> int:
    """最长**真正连续**的数字串长度（中文数字也计入）。

    注意：必须在归一化文本上逐字符扫描。早期实现先把全文数字抽出来拼成一个
    纯数字串再数连续，等价于"全文数字总数"，于是任何包含若干数字的正常消息
    （价格、年份、班号、电话号码片段…）都会被算成"8 位以上长号码"，
    误加 digit_run 信号（实测把家教/招聘信息误判为广告）。
    """
    best = 0
    current = 0
    for char in compact_text(text):
        if char.isdigit() or char in CN_DIGITS:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def edit_distance_within(a: str, b: str, limit: int) -> bool:
    """判断两串编辑距离是否 <= limit（带上界剪枝，避免长文本开销）。"""
    if limit <= 0:
        return a == b
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        best = current[0]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > limit:
            return False
        previous = current
    return previous[-1] <= limit


def fuzzy_contains(text: str, pattern: str, limit: int = 1) -> bool:
    """在 text 中滑动窗口判断是否存在与 pattern 编辑距离 <= limit 的子串。"""
    if not pattern or not text:
        return False
    size = len(pattern)
    if size < 3:
        return False
    for start in range(0, max(1, len(text) - size + 1)):
        window = text[start : start + size + limit]
        if edit_distance_within(pattern, window[:size], limit):
            return True
    return False
