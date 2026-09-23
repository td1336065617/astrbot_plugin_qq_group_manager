"""本地规则层：零成本的廉价过滤，先于 LLM 判定。

多视图匹配（raw / compact / skeleton / pinyin，见 src/normalize.py）+ 多种规则类型：

- literal     精确子串（与旧版行为一致，命中即按规则动作处置）
- normalized  归一化后子串匹配（覆盖"珈裙苓资料""加 群 领 资 料""加-群-领-资-料"）
- regex       正则（raw 与 compact 上各跑一遍）
- fuzzy       编辑距离 <= N（只兜少量错别字；长度 < 3 的模式不启用）
- pinyin      同音匹配（需环境安装 pypinyin，未安装自动跳过）
- 模板规则    "动作词 x 诱饵词"式组合，命中即产生风险分并送审

风险分（0~100）由命中与内置检测累加，送审条件可用 risk>=N 表达：
既拦住变体广告，又让纯闲聊保持"不送审"。

内置跨消息检测：同人 60 秒刷屏、同文案多号刷屏（同一骨架文本在窗口内被多个不同成员发送）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .normalize import (
    fuzzy_contains,
    longest_digit_run,
    normalize,
    pinyin_available,
    to_pinyin_skeleton,
)
from .utils import digest_text

LINK_RE = re.compile(
    r"(https?://|www\.)[^\s]+"
    r"|(?:t\.me|t\.cn|qm\.qq\.com|dwz\.|url\.cn|sourl\.cn|jump\.|bit\.ly|suo\.im)"
    r"|[a-z0-9-]{2,}\s*(?:点|\.)\s*(?:com|cn|net|top|xyz|vip|cc)",
    re.IGNORECASE,
)

CONTACT_RE = re.compile(
    # 强别名（本身不像普通英文单词）允许直接跟账号
    r"(?:扣扣|抠抠|q群|企鹅|微信|weixin|wechat|v信|威信|telegram|纸飞机)"
    r"\s*[:：号]?\s*[0-9a-zA-Z_-]{4,}"
    # 短别名（qq/tg/wx/vx）必须自成词：否则 "cha**tg**ptclaude…" 会误命中
    # （实测事故：AI 玩梗文案因 chatgpt 被判 contact → 送审 → 误判诈骗）
    r"|(?<![0-9a-zA-Z])(?:qq|tg|wx|vx)(?![0-9a-zA-Z])\s*[:：号]?\s*[0-9a-zA-Z_-]{4,}"
    # "v xxx" / "微 xxx"：别名后接分隔符再跟长 ID（允许无冒号，实测漏检写法）
    r"|(?<![0-9a-zA-Z])(?:v|微|薇)(?![0-9a-zA-Z])\s*[:：号]?\s*[0-9a-zA-Z_-]{5,}"
    r"|(?:[vV]\s*[:：]\s*[0-9a-zA-Z_-]{4,})"
    r"|(?:群号|裙号)\s*[:：]?\s*\d{5,}"
    r"|(?:1[3-9]\d{9})"
    r"|(?:wxid_[0-9a-zA-Z_-]+)",
    re.IGNORECASE,
)

#: 招聘/家教语境的结构化字段。群内 CS 学生接家教、学校/机构招老师是正常内容，
#: 其中"加微信 / 联系v:"是**信息本体**而不是引流手段，需要与广告区分开。
#: 单靠"家教"两个字不算，要求命中 >= RECRUIT_MIN_MARKERS 个不同标记。
RECRUIT_MARKERS = (
    "辅导科目",
    "学员情况",
    "时间安排",
    "教员要求",
    "老师薪水",
    "课时费",
    "薪资待遇",
    "招聘",
    "家教",
    "代课",
    "教员",
    "学员",
    "授课",
    "试课",
    "任教",
    "岗位",
    "任职要求",
    "五险一金",
    "本科及以上",
    "学历",
    "小初高",
    "一对一",
    "辅导老师",
    "招聘教师",
    "竞赛教练",
    "杯赛带教",
)

#: 出现这些说明是真实引流/灰产内容：即便带招聘字段也不豁免。
RECRUIT_REJECT = (
    "资源",
    "福利",
    "裸聊",
    "约炮",
    "看片",
    "博彩",
    "彩票",
    "刷单",
    "返利",
    "垫付",
    "外挂",
    "破解",
    "私服",
    "彩金",
    "首充",
    "种子",
    "磁力",
    "防失联",
    "备用群",
    "内部群",
    "免费领取",
    "点击领取",
    "扫码领取",
    "加群领取",
    "日结",
    "代收",
    "跑分",
)

#: 豁免门槛：至少命中几个招聘标记、正文至少多长
RECRUIT_MIN_MARKERS = 2
RECRUIT_MIN_LENGTH = 30

#: leet/中英混排折叠后要重点核对的违规词（研究：手法六 赌b0、色q1ng）
LEET_WATCH_WORDS = (
    "seqing",
    "dubo",
    "bocai",
    "luoliao",
    "yuepao",
    "kanpian",
    "色情",
    "赌博",
    "博彩",
    "裸聊",
    "约炮",
    "看片",
    "援交",
    "彩票",
    "加群",
    "加v",
    "加微",
    "资源",
    "福利",
)

#: 渠道视图：只去掉空白与各种括号，**保留冒号等分隔符**。
#: 混淆写法常把 "V：test_invest" 拆成 "V：\n[ t e s t _ i n v e s t ]"，
#: 而 compact 视图会连冒号一起删掉（→ "vtestinvest"），导致联系方式识别失效。
_CHANNEL_STRIP_RE = re.compile(r"[\s\[\]\(\)（）【】「」｛｝《》<>]")

#: 真实可触达渠道：只有出现这些才谈得上"引流/诈骗实施"
CHANNEL_MARKERS = (
    "链接",
    "网址",
    "二维码",
    "扫码",
    "群号",
    "加群",
    "进群",
    "加我",
    "加你",
    "加v",
    "加薇",
    "加微",
    "微信",
    "威信",
    "qq",
    "扣扣",
    "私聊",
    "私信",
    "私我",
    "公众号",
    "主页",
    "头像",
    "电话",
    "手机号",
    "下载",
    # 加v 家族变体（研究：形近/符号/中英混排写法）
    "加vx",
    "加wx",
    "加qq",
    "薇信",
    "徽信",
    "v信",
    "抠抠",
    "企鹅号",
    "电报",
    # 扣群/叩群：QQ 群的口语写法，骨架视图会把 叩/抠 折叠成 扣
    "扣群",
    "叩群",
)

#: 玩梗语境：模仿诈骗/银行短信格式、经典梗、AI 越狱文案。命中后不应按违规处理。
MEME_PATTERNS = (
    re.compile(r"银行.{0,6}(?:您好|通知).{0,40}余额"),
    re.compile(r"余额为\s*[0-9０-９]{3,}"),
    re.compile(r"预警阈值"),
    re.compile(r"[vV]\s*我\s*[0-9]"),
    re.compile(r"疯狂星期四"),
    re.compile(r"(?:肯德基|KFC).{0,10}银行"),
    re.compile(r"逃逸.{0,30}(?:转账|转我|打钱|赞助)"),
    re.compile(r"(?:0day|0\s*day).{0,40}(?:转账|转我|打钱)"),
    re.compile(r"(?:我是|我是?)\s*(?:GPT|ChatGPT|克劳德|Claude|Gemini)[\s0-9]*[A-Za-z]?", re.IGNORECASE),
    re.compile(r"(?:不设限额|无限额度).{0,10}(?:Codex|额度)"),
)

#: 讨论/引用/提醒语境：只是在谈论诈骗，不是在实施。
DISCUSSION_PATTERNS = (
    re.compile(r"在诈骗|是诈骗|诈骗啊|诈骗吗|诈骗吧|诈骗梗|反诈|防诈|被骗|骗子|典型的诈骗"),
    re.compile(r"标记为(?:最高)?风险"),
    re.compile(r"这是.{0,6}诈骗(?:信息|短信|消息|案例)"),
    re.compile(r"(?:冒充|假冒).{0,8}(?:客服|银行|公安|官方)"),  # 提醒他人注意
)

#: 引导动作词（"去哪里"）
INVITE_VERBS = (
    "加群",
    "进群",
    "入群",
    "拉群",
    "扫码进群",
    "扫码加",
    "扫码",
    "私聊",
    "私信",
    "私我",
    "加我",
    "加v",
    "加薇",
    "加微",
    "威信",
    "联系我",
    "扣我",
    "点击",
    "关注",
    "领取",
    "免费领",
    "群号",
    "二维码",
    "主页",
    "头像",
    "公告",
    "自取",
    "付 费",
    "付费",
    "问管理",
    "找管理",
    "私撩",
    "速进",
    "速上",
    "上车",
    "发车",
    "扣群",
    "进来",
    "加你",
    "扣扣",
    "更多",
)

#: 诱饵/资源词（"给什么"）
BAIT_NOUNS = (
    "资料",
    "资源",
    "福利",
    "教程",
    "答案",
    "兼职",
    "返利",
    "优惠",
    "红包",
    "课程",
    "网课",
    "真题",
    "题库",
    "源码",
    "外挂",
    "彩票",
    "博彩",
    "裸聊",
    "约炮",
    "看片",
    "影视",
    "种子",
    "磁力",
    "合集",
    "目录",
    "破解",
    "会员",
    "惊喜",
    "车牌",
    "新片",
    "老片",
    "链接",
    "地址",
    "资源站",
    "内部",
    "冷门",
    # 灰产/赌博诱饵（研究：黑产黑话）
    "出款",
    "下款",
    "洗码",
    "返水",
    "内部号",
    "带单",
    "跟单",
    "四件套",
    "话费卡",
)

#: 黑话/暗语标记（单独出现不算，但与他项组合即为强信号）
SPAM_MARKERS = (
    "懂的都懂",
    "懂的来",
    "你懂的",
    "先到先得",
    "手慢无",
    "非诚勿扰",
    "永久有效",
    "每日更新",
    "秒发",
    "诚信",
    "限时",
    "安全可靠",
    "拒绝白嫖",
    "白嫖",
    "寂寞",
    "睡不着",
    "深夜",
    "老司机",
    "速上",
    "速进",
    "别声张",
    "不迷路",
    "防失联",
    "备用群",
    "内部群",
    "拉你进群",
    "拉你进",
    "看更多",
    "朋友圈",
    "代找",
    "群主推荐",
    "一包烟钱",
    "全网资源",
    "想要的都",
    "上车",
    "车牌在",
    "稳赚",
    "稳赚不赔",
    "日赚",
    "无风险",
    "全网首发",
    "仅限今天",
    "限量",
    "加我扣扣",
    "加我微信",
)

#: 强标记词：单独出现在**短消息**里即可判定为暗语引流
STRONG_MARKERS = (
    "懂的都懂",
    "懂的来",
    "你懂的",
    "自取",
    "上车",
    "防失联",
    "备用群",
    "内部群",
    "加v",
    "加薇",
    "加微",
    "私聊我",
    "速上",
)

#: 软性开群话术的"催促"词（配合 场景词 + 诱饵词 三件套识别）
OPEN_URGE_WORDS = (
    "手慢无",
    "秒通过",
    "先到先得",
    "名额有限",
    "仅限今天",
    "别错过",
    "速来",
    "抓紧",
    "不多了",
    "限时",
    "限量",
)

#: 开群/拉新场景词。只收"新建/开张"语义：
#: "本群/群内/群里"这类弱场景词会把正常公告（本群资料限时开放，抓紧）也拉进来，故不收。
OPEN_SCENE_WORDS = (
    "新群",
    "开张",
    "开业",
    "新开",
    "建群",
    "新建的群",
)

#: 内置广告模板（可被 KV keywords.templates 覆盖/追加）
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "ad_invite",
        "name": "拉群引流",
        "all_of": [
            {"any_of": list(INVITE_VERBS)},
            {"any_of": [*BAIT_NOUNS, *SPAM_MARKERS]},
        ],
        # 必须有真实渠道才算引流："关注+app"这类泛词组合曾把银行短信梗误判
        "require_channel": True,
        "score": 50,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_open",
        "name": "开群福利引流",
        "all_of": [
            {"any_of": list(OPEN_SCENE_WORDS)},
            {"any_of": list(BAIT_NOUNS)},
            {"any_of": list(OPEN_URGE_WORDS)},
        ],
        # 软性开群话术：没有外链/号码，靠"场景 + 诱饵 + 催促"三件套识别。
        # 三个分组同时成立才命中，且模板只送审、不直接处置。
        "require_channel": False,
        "score": 55,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn"],
    },
    {
        "id": "ad_lure",
        "name": "黑话引流",
        "all_of": [
            {"any_of": list(SPAM_MARKERS)},
            {"any_of": list(BAIT_NOUNS)},
        ],
        "require_channel": True,
        "score": 55,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_night",
        "name": "深夜福利引流",
        "all_of": [
            {
                "any_of": [
                    "深夜",
                    "老司机",
                    "发车",
                    "上车",
                    "车牌",
                    "开车",
                    "寂寞",
                    "睡不着",
                    "看片",
                ]
            },
            {"any_of": ["福利", "资源", "片", "群", "公告", "惊喜", "车牌", "影视"]},
        ],
        "score": 60,
        "category": "色情低俗",
        "severity": 4,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        "id": "ad_piracy",
        "name": "盗版资源贩卖",
        "all_of": [
            {
                "any_of": [
                    "资源",
                    "种子",
                    "磁力",
                    "破解",
                    "会员",
                    "影视",
                    "合集",
                    "打包",
                    "目录",
                    "看片",
                ]
            },
            {
                "any_of": [
                    "进群",
                    "私聊",
                    "管理",
                    "地址",
                    "自取",
                    "付费",
                    "群",
                    "低价",
                    "代找",
                    "秒发",
                ]
            },
        ],
        "score": 55,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_call",
        "name": "召唤式引流",
        "all_of": [
            {
                "any_of": [
                    "加我",
                    "加你",
                    "私我",
                    "扣我",
                    "联系我",
                    "加v",
                    "加微",
                    "扣扣",
                    "威信",
                    "私聊",
                ]
            },
            {"any_of": ["领取", "免费", "资料", "资源", "福利", "群", "号", "看", "更多", "惊喜"]},
        ],
        "score": 50,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_contact",
        "name": "私聊引流",
        "all_of": [
            {"any_of": ["私聊", "私信", "加我", "联系我", "加v", "加微", "加qq", "扣我", "私撩"]},
            {"any_of": [*BAIT_NOUNS, "号", "群"]},
        ],
        "score": 50,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        "id": "ad_parttime",
        "name": "兼职刷单",
        "all_of": [
            {"any_of": ["兼职", "刷单", "日结", "在家做", "轻松赚", "躺赚"]},
            {"any_of": ["日入", "月入", "元", "结算", "押金", "垫付", "返利"]},
        ],
        "score": 55,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        "id": "ad_gamble",
        "name": "赌博引流",
        "all_of": [
            {"any_of": ["博彩", "彩票", "下注", "押注", "赌场", "棋牌", "六合", "时时彩"]},
            {"any_of": ["群", "平台", "app", "网址", "链接", "代理", "返水"]},
        ],
        "score": 60,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        "id": "ad_porn",
        "name": "涉黄引流",
        "all_of": [
            {"any_of": ["裸聊", "约炮", "福利姬", "福利群", "色情", "成人", "av"]},
            {"any_of": ["群", "加", "进", "链接", "app", "资源"]},
        ],
        "score": 65,
        "category": "色情低俗",
        "severity": 5,
        "action": ["warn", "recall", "mute", "report"],
    },
    {
        # 研究：手法五——用 emoji 组合暗示违规（🍑💦🔞🎰💰🃏）。
        # 必须与诱饵/渠道词同时出现才算，避免误伤日常用表情的聊天。
        "id": "ad_emoji",
        "name": "emoji 暗号引流",
        "all_of": [
            {"any_of": ["🔞", "🍑", "🎰", "🃏", "👙", "🍆", "💦", "🎲", "🚗"]},
            {"any_of": [*BAIT_NOUNS, *CHANNEL_MARKERS, "群", "号", "码"]},
        ],
        "score": 45,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        # 研究：黑产黑话（菠菜/跑分/水房/狗推/接码/卡商…）。
        # 这些词本身有正常含义（菠菜=蔬菜、跑分=性能测试、上岸=考研），
        # 因此**只能组合判定**：必须同时出现渠道/交易词。
        "id": "ad_gamble_slang",
        "name": "赌博黑话",
        "all_of": [
            {
                "any_of": [
                    "菠菜盘",
                    "bc盘",
                    "bc平台",
                    "信用盘",
                    "包杀",
                    "包赢",
                    "稳杀",
                    "龙虎",
                    "洗码",
                    "返水",
                    "狗推",
                    "狗庄",
                    "菜农",
                    "水房",
                    "接码",
                    "卡商",
                    "料商",
                    "四件套",
                    "跑分平台",
                    "跑分车队",
                    "代收代付",
                    # 具体组合（避免裸"赌博"误伤讨论；这些词本身已含渠道）
                    "赌博平台",
                    "赌博网站",
                    "博彩平台",
                    "博彩网站",
                    # 中英混排/拼音写法（leet 视图折叠后命中）
                    "赌bo",
                    "dubo",
                    "bocai",
                    "seqing",
                ]
            },
            {
                # 只放"渠道/交易"类词；刻意不含"平台/网站"这类泛词，
                # 否则"接码平台是干什么的"这种正常提问会被误命中。
                "any_of": [
                    "群",
                    "群号",
                    "进群",
                    "加群",
                    "网址",
                    "链接",
                    "下载",
                    "私聊",
                    "加v",
                    "代理",
                    "上车",
                    "出款",
                    "下款",
                    "佣金",
                    "日结",
                    "收米",
                    "押金",
                    "出货",
                    "收单",
                ]
            },
        ],
        "score": 55,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute"],
    },
    {
        # 实测："媺國原裝偉哥 1粒見效 持玖4曉 純兲嘫無副莋鼡 bǎo密髮貨
        # 芣滿噫tuì 佲📞138xxxx"（繁体+异体+全角混淆）
        "id": "ad_medicine",
        "name": "药品保健品广告",
        "all_of": [
            {
                "any_of": [
                    "伟哥",
                    "壮阳",
                    "延时",
                    "持久",
                    "一粒见效",
                    "1粒见效",
                    "无副作用",
                    "保密发货",
                    "无效退款",
                    "不满意退",
                    "原装进口",
                    "男用",
                    "增大",
                    "双效",
                    "印度神油",
                    "万艾可",
                    "希爱力",
                    "保健品",
                ]
            },
            {
                "any_of": [
                    "电话",
                    "订购",
                    "联系",
                    "加v",
                    "微信",
                    "发货",
                    "包邮",
                    "货到付款",
                    "咨询",
                    "抢购",
                ]
            },
        ],
        "score": 55,
        "category": "广告引流",
        "severity": 3,
        "action": ["warn", "recall"],
    },
    {
        # 实测："內募消息 帶沵進圈孖 一対一 仴収益稳萣30%+ 實盤驗証珂查
        # 出金不鎖倉 財務自由 僅限10人 咨詢V 備注理財"（非法荐股/理财诈骗）
        "id": "ad_stock_scam",
        "name": "荐股理财诈骗",
        "all_of": [
            {
                "any_of": [
                    "内幕消息",
                    "内幕",
                    "带盘",
                    "实盘验证",
                    "出金",
                    "锁仓",
                    "财务自由",
                    "月收益",
                    "稳定收益",
                    "稳赚",
                    "名额有限",
                    "仅限",
                    "荐股",
                    "拉升",
                    "建仓",
                    "私募",
                    "游资",
                    "龙头股",
                    "尾盘",
                    "带队",
                ]
            },
            {
                "any_of": [
                    "收益",
                    "理财",
                    "股票",
                    "投资",
                    "资金",
                    "实盘",
                    "名额",
                    "内部",
                    "咨询",
                    "备注",
                    "一对一",
                ]
            },
        ],
        "score": 55,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute"],
        # 必须有真实渠道（链接/联系方式），避免把"内幕""名额有限"这类日常词误判
        "require_channel": True,
    },
    {
        # 研究：网络放贷/征信修复类诈骗话术（同样要求组合，避免误伤讨论）
        "id": "ad_loan",
        "name": "放贷诈骗",
        "all_of": [
            {
                "any_of": [
                    "无抵押",
                    "秒下款",
                    "黑户可下",
                    "包装资料",
                    "征信修复",
                    "征信洗白",
                    "停息挂账",
                    "内部渠道放款",
                    "大额低息",
                ]
            },
            {"any_of": [*CHANNEL_MARKERS, "私聊", "咨询", "加v", "代理", "放款", "下款"]},
        ],
        "score": 55,
        "category": "诈骗赌博",
        "severity": 4,
        "action": ["warn", "recall", "mute"],
    },
]

SCORE_RULES: dict[str, int] = {
    # 玩梗/讨论语境下的分数上限（低于送审阈值，直接放行）
    "meme_cap": 20,
    "exact": 100,
    "normalized": 70,
    "pinyin": 60,
    "fuzzy": 55,
    "template": 45,
    "soft_rule": 20,
    "link": 25,
    "contact": 60,
    "digit_run": 20,
    "digit_run_long": 60,
    "invite_bait": 25,
    "spam_marker": 40,
    "spam_markers_many": 60,
    "bait_many": 60,
    "bait_pair": 40,
    "coded_hint": 25,
    "flood": 20,
    "duplicate_content": 35,
    "repeat_chars": 15,
    "long_text": 10,
    "image": 60,
    #: 中英混排/leet 写法揭示出的违规词（研究：手法六）
    "leet_bypass": 45,
    #: emoji 暗号（研究：手法五）
    "emoji_hint": 20,
    #: 招聘/家教语境下的分数上限（低于送审阈值）
    "recruit_cap": 20,
    #: 招聘/家教文案被多个不同成员在窗口内群发（中介扫群），不豁免
    "recruit_mass_send": 35,
}

SIGNAL_LABELS = {
    "link": "包含外链或短链",
    "link_allowlisted": "链接命中域名白名单（已降权，仍走送审）",
    "contact": "疑似联系方式",
    "digit_run": "含长数字串且伴随引流词",
    "invite_bait": "含拉群动作词与诱饵词",
    "spam_marker": "含引流黑话",
    "spam_markers_many": "多处引流黑话",
    "bait_many": "多个资源/诱饵词",
    "bait_pair": "两个资源/诱饵词",
    "coded_hint": "短消息暗语",
    "flood": "短时间高频发言",
    "duplicate_content": "同一文案多号发送",
    "repeat_chars": "重复字符刷屏",
    "long_text": "超长文本",
    "image": "含图片",
}

MAX_CACHE_GROUPS = 500
FLOOD_WINDOW = 60.0
DUPLICATE_WINDOW = 300.0


@dataclass(slots=True)
class RuleHit:
    """一条命中的规则。"""

    bucket: str
    rule_id: str
    pattern: str
    rule_type: str = "literal"
    matched: str = ""
    actions: list[str] = field(default_factory=list)
    note: str = ""
    score: int = 0
    enforce: bool = False
    """True 表示精确命中硬规则，可直接按规则动作处置；否则只作为送审依据。"""
    category: str = ""
    severity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "rule_id": self.rule_id,
            "pattern": self.pattern,
            "rule_type": self.rule_type,
            "matched": self.matched[:60],
            "actions": list(self.actions),
            "note": self.note,
            "score": self.score,
            "enforce": self.enforce,
        }


@dataclass(slots=True)
class RuleEvaluation:
    """一次规则评估的结果。"""

    hits: list[RuleHit] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    score: int = 0
    views: dict[str, str] = field(default_factory=dict)
    has_link: bool = False
    has_contact: bool = False
    #: 是否出现"真实可触达渠道"（链接/联系方式/群号/二维码等）
    has_channel: bool = False
    #: 是否判定为玩梗/讨论/引用语境（此时不应按违规处理）
    joke_context: bool = False
    #: 是否判定为招聘/家教语境（联系方式是信息本体，不应按引流处理）
    recruit_context: bool = False
    repeated: bool = False
    long_text: bool = False
    flood: bool = False
    duplicate_content: bool = False
    recent_messages: int = 0
    duplicate_senders: int = 0

    @property
    def hard_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "hard"]

    @property
    def soft_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "soft"]

    @property
    def template_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.bucket == "template"]

    @property
    def enforce_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.enforce]

    @property
    def enforce_actions(self) -> list[str]:
        actions: list[str] = []
        for hit in self.enforce_hits:
            for action in hit.actions:
                if action not in actions:
                    actions.append(action)
        return actions

    @property
    def hard_actions(self) -> list[str]:
        """兼容旧调用。"""
        return self.enforce_actions

    @property
    def suspicious(self) -> bool:
        return bool(self.hits or self.signals)

    def rule_summary(self) -> str:
        """只含"规则/模板命中"的摘要，用于 rule_hit 送审条件（不含内置启发式）。"""
        return "、".join(
            (hit.pattern if hit.rule_type == "literal" else hit.rule_type + ":" + hit.pattern)
            for hit in self.hits[:6]
        )

    def summary(self) -> str:
        """给提示词用的可疑点摘要。"""
        parts: list[str] = []
        for hit in self.hits[:6]:
            label = hit.pattern if hit.rule_type == "literal" else hit.rule_type + ":" + hit.pattern
            parts.append(label)
        for key in ("link", "contact", "digit_run", "invite_bait", "duplicate_content", "flood"):
            if key in self.signals:
                parts.append(SIGNAL_LABELS.get(key, key))
        if self.repeated:
            parts.append("重复字符刷屏")
        if self.long_text:
            parts.append("超长文本")
        if self.duplicate_senders >= 2:
            parts.append("同一文案被 " + str(self.duplicate_senders) + " 个成员发送")
        return "、".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "signals": dict(self.signals),
            "score": self.score,
            "views": dict(self.views),
            "summary": self.summary(),
        }


class RuleEngine:
    """规则求值 + 轻量刷屏/重复文案统计（纯内存，重启即清空）。"""

    def __init__(
        self,
        keywords: dict[str, list[dict[str, Any]]] | None = None,
        *,
        templates: list[dict[str, Any]] | None = None,
        homoglyph: dict[str, str] | None = None,
        auto_enforce_normalized: bool = False,
        fuzzy_max_distance: int = 1,
        pinyin_enabled: bool = False,
    ) -> None:
        self._compiled: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        self._templates: list[dict[str, Any]] = []
        self._homoglyph: dict[str, str] = {}
        self._recent: dict[str, deque[float]] = {}
        self._content: dict[str, deque[tuple[float, str]]] = {}
        self._clock = time.monotonic
        self.auto_enforce_normalized = bool(auto_enforce_normalized)
        self.fuzzy_max_distance = max(0, int(fuzzy_max_distance))
        self.pinyin_enabled = bool(pinyin_enabled) and pinyin_available()
        self.reload(keywords or {}, templates=templates, homoglyph=homoglyph)

    def configure(
        self,
        *,
        auto_enforce_normalized: bool | None = None,
        fuzzy_max_distance: int | None = None,
        pinyin_enabled: bool | None = None,
    ) -> None:
        """热更新运行参数（来自插件配置）。"""
        if auto_enforce_normalized is not None:
            self.auto_enforce_normalized = bool(auto_enforce_normalized)
        if fuzzy_max_distance is not None:
            self.fuzzy_max_distance = max(0, int(fuzzy_max_distance))
        if pinyin_enabled is not None:
            self.pinyin_enabled = bool(pinyin_enabled) and pinyin_available()

    def reload(
        self,
        keywords: dict[str, list[dict[str, Any]]],
        *,
        templates: list[dict[str, Any]] | None = None,
        homoglyph: dict[str, str] | None = None,
    ) -> None:
        """重新编译规则、模板与形近字表（WebUI 保存后调用）。"""
        compiled: dict[str, list[dict[str, Any]]] = {"hard": [], "soft": []}
        for bucket in ("hard", "soft"):
            for item in keywords.get(bucket) or []:
                if not isinstance(item, dict) or not item.get("pattern"):
                    continue
                kind = str(item.get("type") or "literal").lower()
                pattern = str(item.get("pattern"))
                regex = None
                if kind == "regex":
                    try:
                        regex = re.compile(pattern, re.IGNORECASE)
                    except re.error:
                        continue
                compiled[bucket].append(
                    {
                        "id": str(item.get("id") or pattern[:24]),
                        "kind": kind,
                        "pattern": pattern,
                        "pattern_normalized": None,
                        "pattern_pinyin": None,
                        "regex": regex,
                        "actions": [
                            str(action)
                            for action in (item.get("action") or [])
                            if isinstance(action, (str, int))
                        ],
                        "scope": str(item.get("scope") or "all"),
                        "enabled": bool(item.get("enabled", True)),
                        "note": str(item.get("note") or ""),
                    }
                )
        self._compiled = compiled
        if homoglyph is not None:
            self._homoglyph = {
                str(key): str(value)
                for key, value in (homoglyph or {}).items()
                if str(key) and str(value)
            }
        self._templates = self._compile_templates(templates)

    @staticmethod
    def _compile_templates(templates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        # None：使用内置模板；[]（显式空列表）：完全关闭模板规则
        items = BUILTIN_TEMPLATES if templates is None else templates
        compiled: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            groups = [
                [str(word) for word in (group.get("any_of") or []) if str(word)]
                for group in (item.get("all_of") or [])
                if isinstance(group, dict)
            ]
            groups = [group for group in groups if group]
            if not groups:
                continue
            compiled.append(
                {
                    "id": str(item.get("id") or "template"),
                    "name": str(item.get("name") or item.get("id") or "广告模板"),
                    "groups": groups,
                    "score": int(item.get("score") or SCORE_RULES["template"]),
                    "category": str(item.get("category") or "广告引流"),
                    "severity": int(item.get("severity") or 3),
                    "actions": [
                        str(action) for action in (item.get("action") or []) if str(action)
                    ],
                    "enabled": bool(item.get("enabled", True)),
                    "require_channel": bool(item.get("require_channel", False)),
                }
            )
        return compiled

    # ------------------------------------------------------------------
    def note_message(self, group_id: str, member_openid: str) -> int:
        """记录一条发言，返回该成员最近 60 秒内的发言数。"""
        if not member_openid:
            return 0
        key = group_id + ":" + member_openid
        now = self._clock()
        bucket = self._recent.get(key)
        if bucket is None:
            if len(self._recent) > MAX_CACHE_GROUPS * 100:
                self._recent.clear()
            bucket = deque(maxlen=200)
            self._recent[key] = bucket
        bucket.append(now)
        cutoff = now - FLOOD_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket)

    def note_content(
        self,
        group_id: str,
        text: str,
        member_openid: str,
        *,
        window: float = DUPLICATE_WINDOW,
    ) -> int:
        """记录一条消息内容，返回窗口内发送同一文案的**不同成员数**。"""
        if not text or not member_openid:
            return 0
        key = group_id + ":" + digest_text(text, limit=256)
        now = self._clock()
        bucket = self._content.get(key)
        if bucket is None:
            if len(self._content) > MAX_CACHE_GROUPS * 100:
                self._content.clear()
            bucket = deque(maxlen=50)
            self._content[key] = bucket
        bucket.append((now, member_openid))
        cutoff = now - max(30.0, float(window))
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()
        return len({item[1] for item in bucket})

    # ------------------------------------------------------------------
    def _match_rule(self, rule: dict[str, Any], views: Any) -> tuple[str, bool]:
        """返回 (命中片段, 是否"原样精确命中")。空串表示未命中。"""
        kind = rule["kind"]
        pattern = rule["pattern"]
        if kind == "regex":
            for view in (views.raw, views.compact, views.skeleton):
                if view:
                    found = rule["regex"].search(view)
                    if found:
                        return found.group(0), False
            return "", False
        if kind == "pinyin":
            if not self.pinyin_enabled or not views.pinyin:
                return "", False
            needle = rule.get("pattern_pinyin")
            if needle is None:
                needle = to_pinyin_skeleton(pattern)
                rule["pattern_pinyin"] = needle
            return (pattern, False) if needle and needle in views.pinyin else ("", False)
        if kind == "fuzzy":
            if self.fuzzy_max_distance <= 0:
                return "", False
            for view in (views.compact, views.skeleton):
                if view and fuzzy_contains(view, pattern, self.fuzzy_max_distance):
                    return pattern, False
            return "", False
        if kind == "normalized":
            needle = self._normalized_pattern(rule)
            if not needle:
                return "", False
            for view in (views.compact, views.skeleton):
                if view and needle in view:
                    return needle, False
            return self._match_pinyin(rule, views)

        # literal：先按原样子串（精确命中），再退回归一化/骨架（变体命中）
        lowered = (views.raw or "").lower()
        if pattern.lower() in lowered:
            return pattern, True
        needle = self._normalized_pattern(rule)
        if needle:
            for view in (views.compact, views.skeleton):
                if view and needle in view:
                    return needle, False
        return self._match_pinyin(rule, views)

    def _match_pinyin(self, rule: dict[str, Any], views: Any) -> tuple[str, bool]:
        """同音兜底：把规则词转成拼音骨架后在消息的拼音视图里找（如 jiaqunlingziliao）。"""
        if not self.pinyin_enabled or not views.pinyin:
            return "", False
        needle = rule.get("pattern_pinyin")
        if needle is None:
            needle = to_pinyin_skeleton(rule["pattern"])
            rule["pattern_pinyin"] = needle
        if needle and needle in views.pinyin:
            return str(needle), False
        return "", False

    def _normalized_pattern(self, rule: dict[str, Any]) -> str:
        """规则的归一化形式（带缓存）。"""
        needle = rule.get("pattern_normalized")
        if needle is None:
            needle = normalize(rule["pattern"], homoglyph=self._homoglyph).skeleton
            rule["pattern_normalized"] = needle
        return str(needle or "")

    def evaluate(
        self,
        text: str,
        *,
        group_id: str = "",
        flood_threshold: int = 8,
        recent_messages: int = 0,
        duplicate_senders: int = 0,
        duplicate_members: int = 3,
        allowlisted: bool = False,
    ) -> RuleEvaluation:
        """求值：多视图规则匹配 + 模板匹配 + 内置检测 + 风险分。

        allowlisted=True 表示文本中的链接全部命中域名白名单：此时不计 link 分、
        不置 has_link，但仍走完整规则、审计与送审判断（**只降权，不豁免**）。
        """
        views = normalize(text or "", homoglyph=self._homoglyph, with_pinyin=self.pinyin_enabled)
        result = RuleEvaluation(
            views={
                "raw": views.raw,
                "compact": views.compact,
                "skeleton": views.skeleton,
                "pinyin": views.pinyin,
                "leet": getattr(views, "leet", ""),
            }
        )
        score = 0

        for bucket in ("hard", "soft"):
            for rule in self._compiled[bucket]:
                if not rule["enabled"] or rule["scope"] not in ("all", group_id):
                    continue
                matched, exact_raw = self._match_rule(rule, views)
                if not matched:
                    continue
                kind = rule["kind"]
                if kind == "literal":
                    # 变体命中（形近字/插符号/全角）只作为送审依据，不直接处置
                    contribution = SCORE_RULES["exact"] if exact_raw else SCORE_RULES["normalized"]
                elif bucket == "soft" and kind == "regex":
                    contribution = SCORE_RULES["soft_rule"]
                elif kind == "normalized":
                    contribution = SCORE_RULES["normalized"]
                elif kind == "pinyin":
                    contribution = SCORE_RULES["pinyin"]
                elif kind == "fuzzy":
                    contribution = SCORE_RULES["fuzzy"]
                else:
                    contribution = SCORE_RULES["soft_rule"]
                enforce = bucket == "hard" and (
                    exact_raw or (self.auto_enforce_normalized and kind != "regex")
                )
                score += contribution
                result.hits.append(
                    RuleHit(
                        bucket=bucket,
                        rule_id=rule["id"],
                        pattern=rule["pattern"],
                        rule_type=kind,
                        matched=matched,
                        actions=list(rule["actions"]),
                        note=rule["note"],
                        score=contribution,
                        enforce=enforce,
                    )
                )

        # 渠道存在性提前计算：供模板的 require_channel 判断（链接/联系方式/渠道词）
        channel_view = _CHANNEL_STRIP_RE.sub("", views.raw or "")
        link_detected = bool(
            LINK_RE.search(views.raw or "") or LINK_RE.search(channel_view)
        )
        link_present = link_detected
        if link_present and allowlisted:
            # 白名单内链接：不计 link 分、不置 has_link，但下游渠道语义保持不变。
            link_present = False
            result.signals["link_allowlisted"] = True
        contact_present = bool(
            CONTACT_RE.search(views.raw or "")
            or CONTACT_RE.search(views.compact)
            or CONTACT_RE.search(channel_view)
        )
        channel_present = link_present or contact_present or any(
            word in views.skeleton or word in views.compact
            for word in CHANNEL_MARKERS
        )
        for template in self._templates:
            if not template["enabled"]:
                continue
            if template.get("require_channel") and not channel_present:
                continue
            matched_words: list[str] = []
            ok = True
            for group in template["groups"]:
                found = ""
                for word in group:
                    needle = normalize(word, homoglyph=self._homoglyph).skeleton or word.lower()
                    if needle and (
                        needle in views.skeleton
                        or needle in views.compact
                        or needle in getattr(views, "leet", "")
                        or word.lower() in (views.raw or "").lower()
                    ):
                        found = word
                        break
                if not found or found in matched_words:
                    ok = False
                    break
                matched_words.append(found)
            if ok:
                score += template["score"]
                result.hits.append(
                    RuleHit(
                        bucket="template",
                        rule_id=template["id"],
                        pattern=template["name"] + "：" + "+".join(matched_words),
                        rule_type="template",
                        matched="+".join(matched_words),
                        actions=list(template["actions"]),
                        note=template["category"],
                        score=template["score"],
                        enforce=False,
                        category=template["category"],
                        severity=template["severity"],
                    )
                )

        if link_present:
            result.has_link = True
            result.signals["link"] = SCORE_RULES["link"]
        if contact_present:
            result.has_contact = True
            result.signals["contact"] = SCORE_RULES["contact"]
        digit_run = longest_digit_run(text or "")
        if digit_run >= 6 and any(
            word in views.skeleton for word in ("加", "群", "资料", "领", "联系")
        ):
            # 6~7 位只是弱信号；8 位以上（接近手机号/QQ号）单独就足以送审
            result.signals["digit_run"] = (
                SCORE_RULES["digit_run_long"] if digit_run >= 8 else SCORE_RULES["digit_run"]
            )
        skeleton = views.skeleton
        compact = views.compact
        verb_hits = [word for word in INVITE_VERBS if word in skeleton]
        bait_hits = [word for word in BAIT_NOUNS if word in skeleton]
        marker_hits = [word for word in SPAM_MARKERS if word in skeleton or word in compact]
        # 真实渠道判定：链接/联系方式，或文本中出现渠道词（群号、加v、二维码…）。
        # 无渠道时"动词+诱饵"不足以判定引流，避免玩笑/短信梗被误判。
        has_channel = bool(link_detected or result.has_contact) or any(
            word in skeleton or word in compact for word in CHANNEL_MARKERS
        )
        result.has_channel = has_channel
        if verb_hits and bait_hits and has_channel:
            result.signals["invite_bait"] = SCORE_RULES["invite_bait"]
        if marker_hits:
            result.signals["spam_marker"] = SCORE_RULES["spam_marker"]
        if len(marker_hits) >= 2:
            result.signals["spam_markers_many"] = SCORE_RULES["spam_markers_many"]
        if len(bait_hits) >= 3:
            result.signals["bait_many"] = SCORE_RULES["bait_many"]
        elif len(bait_hits) == 2:
            result.signals["bait_many"] = SCORE_RULES["bait_pair"]
        # 短消息 + 强暗语（你懂的 / 懂的都懂 / 上车 / 车牌…）才算暗语引流，
        # "深夜""睡不着"这类弱标记不算，避免把"深夜加班"误判。
        if (
            any(word in skeleton or word in compact for word in STRONG_MARKERS)
            and len(skeleton) <= 12
        ):
            result.signals["coded_hint"] = SCORE_RULES["coded_hint"]
        if recent_messages >= max(1, flood_threshold):
            result.flood = True
            result.recent_messages = recent_messages
            result.signals["flood"] = SCORE_RULES["flood"]
        if duplicate_senders >= max(2, duplicate_members):
            result.duplicate_content = True
            result.duplicate_senders = duplicate_senders
            result.signals["duplicate_content"] = SCORE_RULES["duplicate_content"]
        # 中英混排/leet（研究：手法六）：折叠后能看到的违规词，说明原文在规避
        leet_text = str((result.views or {}).get("leet") or "")
        if leet_text:
            leet_hits = [
                word
                for word in LEET_WATCH_WORDS
                if word in leet_text and word not in skeleton
            ]
            if leet_hits:
                result.signals["leet_bypass"] = SCORE_RULES["leet_bypass"]
        if re.search(r"(.)\1{6,}", text or ""):
            result.repeated = True
            result.signals["repeat_chars"] = SCORE_RULES["repeat_chars"]
        if len(text or "") >= 600:
            result.long_text = True
            result.signals["long_text"] = SCORE_RULES["long_text"]

        # 只累加数值型信号：link_allowlisted 等布尔留痕不参与打分。
        score += sum(
            value
            for value in result.signals.values()
            if isinstance(value, int) and not isinstance(value, bool)
        )

        # 玩梗 / 讨论 / 引用语境识别：
        # - 模仿银行短信、经典梗（v我50）、AI 越狱文案 → 不是违规实施；
        # - "你怎么在诈骗啊""这是典型的诈骗信息" → 只是谈论或提醒。
        # 命中且**没有任何真实渠道**时压低分数，避免送 LLM 复审后被按字面判违规。
        raw_text = text or ""

        # 招聘/家教语境：结构化字段 + 无外链 + 无灰产诱饵 → 按正常内容处理。
        # 例：辅导科目/学员情况/时间安排/教员要求/老师薪水 + 联系v:xxx
        recruit_markers = [
            word for word in RECRUIT_MARKERS if word in skeleton or word in compact
        ]
        recruit_like = (
            len(recruit_markers) >= RECRUIT_MIN_MARKERS
            and len(raw_text) >= RECRUIT_MIN_LENGTH
            and not link_detected
            and not any(word in skeleton or word in compact for word in RECRUIT_REJECT)
        )
        if recruit_like and result.duplicate_content:
            # 同一条招聘/家教文案在窗口内被**多个不同成员**发送 = 中介扫群群发，
            # 与"个人发一条家教帖"性质不同，因此不豁免；额外给出可解释的信号。
            result.signals["recruit_mass_send"] = SCORE_RULES["recruit_mass_send"]
        elif recruit_like:
            result.recruit_context = True
            result.signals["recruit_context"] = 0
            score = min(score, SCORE_RULES.get("recruit_cap", 20))
            # 家教正文里的"加微+网课"是联系方式与课程本身，撤销广告类模板命中
            result.hits = [
                hit
                for hit in result.hits
                if hit.category not in ("广告引流", "诈骗赌博")
            ]

        meme_hit = any(pattern.search(raw_text) for pattern in MEME_PATTERNS)
        discuss_hit = any(pattern.search(raw_text) for pattern in DISCUSSION_PATTERNS)
        if (meme_hit or discuss_hit) and not result.recruit_context:
            result.joke_context = True
            result.signals["meme_context" if meme_hit else "discussion_context"] = 0
            if not (result.has_link or result.has_contact):
                score = min(score, SCORE_RULES.get("meme_cap", 20))
        result.score = min(100, score)
        return result

    def with_flood(
        self, evaluation: RuleEvaluation, count: int, threshold: int = 8
    ) -> RuleEvaluation:
        """补充刷屏判定（兼容旧调用：先 note_message 再补分）。"""
        evaluation.recent_messages = count
        evaluation.flood = threshold > 0 and count >= threshold
        if evaluation.flood and "flood" not in evaluation.signals:
            evaluation.signals["flood"] = SCORE_RULES["flood"]
            evaluation.score = min(100, evaluation.score + SCORE_RULES["flood"])
        return evaluation
