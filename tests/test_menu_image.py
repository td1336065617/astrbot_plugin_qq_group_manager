"""菜单图片化：文字版不变 + HTML 渲染 + 失败回落 + 接线。"""
from __future__ import annotations

import asyncio
import logging

from src.commands import menu_text
from src.menu_render import menu_html, split_item

import test_mention_targets as mt

GOLDEN_MENU = "\n".join(
    [
        "QQ群管理",
        "──────────────",
        "所有人",
        "• 群信息 ─ 本群档案与机器人在群状态",
        "• 审核状态 ─ 审核开关、模式与我的豁免状态",
        "• 申诉 <理由> ─ 对被处置的消息提出申诉（回复原消息）",
        "• 群管理菜单 ─ 显示本菜单",
        "──────────────",
        "群主 / 群管理员",
        "• 审核开启 / 审核关闭",
        "• 审核模式 严格/标准/宽松/仅记录",
        "• 审核阈值 0.0-1.0",
        "• 关键词 添加/删除/列表 · 信任 @某人 · 取消信任 @某人",
        "• 禁言 @某人 [时长] · 解禁 @某人 · 撤回（引用消息，2 分钟内、限普通成员消息）",
        "• 审核日志 [条数] · 审核统计 [今日/7天]",
        "• 申诉通过 / 申诉驳回 [理由] ─ 回复申诉消息处理",
        "• 入群审核 开启/关闭/模式 <模式> · 入群申请",
        "• 入群通过 <序号> · 入群拒绝 <序号> [理由]",
        "• 黑名单 添加/移除/列表",
        "• dry-run ─ 查看当前运行模式；dry-run 关闭/开启 ─ 切换实际处置",
        "──────────────",
        "AstrBot 管理员",
        "• 群管理自检 ─ 平台能力探测",
        "• 群管理配置 ─ 打开管理台指引",
        "──────────────",
        "提示：指令为全匹配，可带 / 前缀。",
    ]
)


def test_menu_text_is_byte_stable():
    """重构为数据驱动后，文字菜单必须逐字不变（外部契约）。"""
    assert menu_text() == GOLDEN_MENU


def test_menu_html_contains_all_sections():
    html = menu_html(
        version="0.14.13", requested_at="2026-10-09 14:30", requester="道", group_name="测试群"
    )
    for key in ("所有人", "群主 / 群管理员", "AstrBot 管理员", "群管理自检", "撤回（引用消息"):
        assert key in html
    assert "v0.14.13" in html and "请求人 道" in html and "测试群" in html
    assert html.startswith("<!DOCTYPE html>") and html.endswith("</html>")


def test_menu_html_escapes_dynamic_text():
    html = menu_html(requester="<b>坏</b>&\"")
    assert "&lt;b&gt;坏&lt;/b&gt;" in html
    assert "<b>坏</b>" not in html


def test_split_item_prefers_bullet_separator():
    assert split_item("群信息 ─ 本群档案与机器人在群状态") == ("群信息", "本群档案与机器人在群状态")
    assert split_item("申诉通过 / 申诉驳回 [理由] ─ 回复申诉消息处理") == (
        "申诉通过 / 申诉驳回 [理由]",
        "回复申诉消息处理",
    )


def test_split_item_falls_back_to_first_space():
    assert split_item("审核模式 严格/标准/宽松/仅记录") == ("审核模式", "严格/标准/宽松/仅记录")
    assert split_item("关键词 添加/删除/列表 · 信任 @某人") == ("关键词", "添加/删除/列表 · 信任 @某人")
    assert split_item("审核开启 / 审核关闭") == ("审核开启 / 审核关闭", "")


def test_split_item_keeps_long_text_as_description():
    command, description = split_item("这是一条没有指令前缀的很长很长的说明文字内容")
    assert command == "" and description.startswith("这是一条")


def _plugin(store, render):
    main = mt.load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin.store = store
    plugin.logger = logging.getLogger("qqgm-menu-image")
    plugin.html_render = render
    return plugin


def test_menu_image_returns_rendered_url():
    seen: list[str] = []

    async def render(tmpl, data, return_url=True, options=None):
        seen.append(tmpl)
        return "https://example.com/menu.jpg"

    plugin = _plugin(mt.make_store(), render)
    event = mt.FakeEvent("群管理菜单")
    url = asyncio.run(plugin._menu_image(event, "道"))
    assert url == "https://example.com/menu.jpg"
    assert seen and "群管理菜单" in seen[0] and "<html" in seen[0]


def test_menu_image_falls_back_to_text_on_error():
    async def render(tmpl, data, return_url=True, options=None):
        raise RuntimeError("t2i 不可用")

    plugin = _plugin(mt.make_store(), render)
    assert asyncio.run(plugin._menu_image(mt.FakeEvent("群管理菜单"), "道")) == ""


def test_menu_image_respects_off_switch():
    class _Off:
        def get_setting(self, key, default=None):
            return False if key == "menu_image" else default

    async def render(tmpl, data, return_url=True, options=None):
        raise AssertionError("开关关闭时不应调用渲染")

    plugin = _plugin(_Off(), render)
    assert asyncio.run(plugin._menu_image(mt.FakeEvent("群管理菜单"), "道")) == ""


class _MenuEvent(mt.FakeEvent):
    """补上指令路径需要的事件接口。"""

    def __init__(self, text: str, **kwargs) -> None:
        super().__init__(text, **kwargs)
        self.images: list[str] = []
        self.texts: list[str] = []

    def is_admin(self) -> bool:
        return True

    def image_result(self, url):
        self.images.append(url)
        return ("image", url)

    def plain_result(self, text):
        self.texts.append(text)
        return ("text", text)


class _SilentApi:
    available = False
    kind = "official"

    def bind_event(self, event) -> None:
        return None


def test_menu_command_sends_image_not_text():
    """接线：群里发「群管理菜单」应发出图片，而不是文字。"""

    async def render(tmpl, data, return_url=True, options=None):
        return "https://example.com/menu.jpg"

    main = mt.load_main()
    plugin = object.__new__(main.QQGroupManager)
    plugin.store = mt.make_store()
    plugin.api = _SilentApi()
    plugin.audit = None
    plugin.logger = logging.getLogger("qqgm-menu-wire")
    plugin._platform_id = ""
    plugin._recent_msgs = {}
    plugin.html_render = render

    async def run() -> _MenuEvent:
        event = _MenuEvent("群管理菜单", msg_id="MSG-MENU")
        async for _ in main.QQGroupManager.on_group_message(plugin, event):
            pass
        return event

    event = asyncio.run(run())
    assert event.images == ["https://example.com/menu.jpg"]
    assert "QQ群管理" not in "".join(event.texts)
