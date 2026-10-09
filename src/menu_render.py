"""群管理菜单的图片渲染（HTML 卡片，走 AstrBot 的 html_render）。

文字版见 `src/commands.py:menu_text()`：两处共用 `MENU_SECTIONS`，不会走偏。
渲染失败/关闭时由调用方回落成文字菜单。
"""

from __future__ import annotations

from html import escape

from .commands import MENU_SECTIONS, MENU_TIP, MENU_TITLE

#: 卡片宽度（px）
CARD_WIDTH = 660
#: 指令与说明的分隔符（文字版里的 " ─ "）
ITEM_SEP = " ─ "

_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #eef1f6; }
  .card {
    width: %(width)dpx; padding: 24px 28px 18px;
    background: #ffffff;
    border: 1px solid #e3e8f0; border-radius: 20px;
    box-shadow: 0 10px 30px rgba(23, 43, 77, 0.10);
    color: #1f2937;
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  .head { display: flex; align-items: center; gap: 14px; }
  .logo {
    width: 54px; height: 54px; border-radius: 15px; flex: 0 0 auto;
    background: linear-gradient(140deg, #4f8dff 0%%, #2f6bff 100%%);
    color: #ffffff; font-size: 27px; line-height: 54px; text-align: center;
    box-shadow: 0 6px 16px rgba(47, 107, 255, 0.28);
  }
  .title h1 { font-size: 25px; font-weight: 700; letter-spacing: 0.3px; }
  .title .sub { margin-top: 4px; font-size: 12.5px; color: #8a93a5; }
  .rule { margin: 18px 0 4px; height: 1px; background: #eef1f6; }
  .sec { margin-top: 18px; }
  .sec .sec-title {
    display: flex; align-items: center; gap: 8px;
    font-size: 15px; font-weight: 600; color: #2f6bff;
  }
  .sec .sec-title .bar { width: 4px; height: 15px; border-radius: 2px; background: #2f6bff; }
  .sec.admin .sec-title { color: #c47f12; }
  .sec.admin .sec-title .bar { background: #f0a929; }
  .rows { margin-top: 9px; display: table; width: 100%%; border-spacing: 0 3px; }
  .row { display: table-row; }
  .row .cmd, .row .desc { display: table-cell; vertical-align: middle; padding: 1px 0; }
  .row .cmd { width: 176px; padding-right: 12px; white-space: nowrap; }
  .row .cmd span {
    display: inline-block; padding: 3px 9px; border-radius: 8px;
    background: #eef4ff; color: #2358d8;
    font-size: 13.5px; font-weight: 600;
  }
  .sec.admin .row .cmd span { background: #fff5e2; color: #b0740c; }
  .row .desc { font-size: 14px; line-height: 1.5; color: #4b5563; }
  .foot {
    margin-top: 20px; padding-top: 14px; border-top: 1px solid #eef1f6;
    display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
    font-size: 12.5px; color: #8a93a5;
  }
"""


#: 指令片段最长字符数（超过则整条当说明，避免出现超长 chip）
MAX_COMMAND_CHARS = 18


def split_item(item: str) -> tuple[str, str]:
    """把条目拆成（指令, 说明）。

    优先按文字版的 " ─ " 拆；没有则按第一个空格拆（如「审核模式 严格/标准/宽松」）；
    指令片段过长或拆不出时，整条作为说明（不显示 chip）。
    """
    text = item.strip()
    if ITEM_SEP in text:
        command, _, description = text.partition(ITEM_SEP)
        command, description = command.strip(), description.strip()
        if command and description and len(command) <= MAX_COMMAND_CHARS:
            return command, description
        return "", text
    head, sep, tail = text.partition(" ")
    if sep and tail.strip() and len(head) <= MAX_COMMAND_CHARS:
        # 「审核开启 / 审核关闭」这种并列写法整体作为指令，不分列
        if tail.lstrip().startswith("/") and len(text) <= MAX_COMMAND_CHARS:
            return text, ""
        return head.strip(), tail.strip()
    return "", text


def menu_html(
    version: str = "",
    requested_at: str = "",
    requester: str = "",
    group_name: str = "",
) -> str:
    """把菜单数据渲染成一张卡片图（HTML 交给 t2i 服务出图）。"""
    sections = []
    for title, items in MENU_SECTIONS:
        is_admin = title.startswith("AstrBot")
        rows = []
        for item in items:
            command, description = split_item(item)
            chip = f"<span>{escape(command)}</span>" if command else ""
            rows.append(
                f'<div class="row"><div class="cmd">{chip}</div>'
                f'<div class="desc">{escape(description)}</div></div>'
            )
        cls = "sec admin" if is_admin else "sec"
        sections.append(
            f'<div class="{cls}"><div class="sec-title"><i class="bar"></i>'
            f"{escape(title)}</div>"
            f'<div class="rows">{"".join(rows)}</div></div>'
        )
    sub_bits = [f"v{escape(version)}" if version else ""]
    if group_name:
        sub_bits.append(escape(group_name))
    if requester:
        sub_bits.append(f"请求人 {escape(requester)}")
    subtitle = " · ".join(bit for bit in sub_bits if bit)
    body = "".join(sections)
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{_STYLE % {'width': CARD_WIDTH}}</style></head><body>"
        '<div class="card">'
        '<div class="head"><div class="logo">🛡️</div>'
        f'<div class="title"><h1>{escape(MENU_TITLE)}</h1>'
        f'<div class="sub">{subtitle}</div></div></div>'
        '<div class="rule"></div>'
        f"{body}"
        '<div class="foot">'
        f"<span>{escape(MENU_TIP)}</span>"
        f'<span>{escape(requested_at)}</span>'
        "</div></div></body></html>"
    )
