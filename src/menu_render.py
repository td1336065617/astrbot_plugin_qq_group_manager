"""群管理菜单的图片渲染（HTML 卡片，走 AstrBot 的 html_render）。

文字版见 `src/commands.py:menu_text()`：两处共用 `MENU_SECTIONS`，不会走偏。
渲染失败/关闭时由调用方回落成文字菜单。

视觉风格借鉴了同类群管插件的菜单观感（深紫底 + 粉/青径向光晕 + 磨砂白面板 + 粉色强调），
仅借用观感与配色，数据与结构仍来自本插件自己的 MENU_SECTIONS。
"""

from __future__ import annotations

from html import escape

from .commands import MENU_SECTIONS, MENU_TIP, MENU_TITLE

#: 卡片宽度（px）
CARD_WIDTH = 1080
#: 指令与说明的分隔符（文字版里的 " ─ "）
ITEM_SEP = " ─ "

_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { background: #21162d; }
  body {
    color: #563b65;
    font-family: "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "Microsoft YaHei", sans-serif;
    font-variant-east-asian: simplified;
    text-rendering: optimizeLegibility;
    -webkit-font-smoothing: antialiased;
  }
  .emoji { font-family: "Noto Color Emoji", "Segoe UI Emoji", sans-serif; font-variant-emoji: emoji; }
  .page {
    width: 100%%; max-width: %(width)dpx; margin: 0 auto; padding: 46px 60px 56px; position: relative; overflow: hidden;
    background:
      radial-gradient(circle at 92%% 8%%, rgba(255,177,218,.26), transparent 24%%),
      radial-gradient(circle at 6%% 90%%, rgba(145,223,247,.20), transparent 25%%),
      linear-gradient(135deg, #281936 0%%, #49264d 55%%, #203a4a 100%%);
  }
  .page:before {
    content: ""; position: absolute; width: 260px; height: 260px; right: -130px; top: 40px;
    border: 1px solid rgba(255,215,237,.30); border-radius: 50%%;
    box-shadow: 0 0 0 18px rgba(255,215,237,.07), 0 0 0 42px rgba(162,225,248,.05);
    pointer-events: none;
  }
  .page:after {
    content: "✿"; position: absolute; right: 96px; top: 84px; color: rgba(255,225,241,.62);
    font-size: 54px; line-height: 1; pointer-events: none;
  }
  .title {
    color: #fff7fb; font-size: 38px; font-weight: 900; line-height: 1.45;
    margin-bottom: 26px; letter-spacing: 1px; text-shadow: 0 3px 18px rgba(243,132,190,.42);
  }
  .title:before {
    content: "✦ QQ GROUP MANAGER ✦"; display: block; margin-bottom: 8px;
    color: #ffd5e8; font-size: 17px; font-weight: 700; letter-spacing: 3px;
    text-shadow: 0 2px 10px rgba(243,132,190,.35);
  }
  .title .sub {
    margin-top: 10px; color: #f0d9ec; font-size: 18px; font-weight: 500; letter-spacing: .6px;
  }
  .panel {
    position: relative; padding: 34px 38px 38px; border-radius: 22px;
    background: linear-gradient(145deg, rgba(255,252,255,.985), rgba(252,244,252,.965));
    box-shadow: 0 18px 34px rgba(28,10,44,.30), inset 0 0 24px rgba(255,255,255,.70);
  }
  .panel:before {
    content: ""; position: absolute; left: 30px; right: 30px; top: 0; height: 3px; border-radius: 99px;
    background: linear-gradient(90deg, rgba(243,132,190,0), #f384be 22%%, #a2e1f8 78%%, rgba(162,225,248,0));
  }
  .sec { margin-top: 26px; }
  .sec:first-child { margin-top: 4px; }
  .sec-title {
    display: flex; align-items: center; gap: 10px;
    color: #71466f; font-size: 22px; font-weight: 800; letter-spacing: .6px;
  }
  .sec-title .dot {
    width: 10px; height: 10px; border-radius: 50%%; flex: 0 0 auto;
    background: linear-gradient(140deg, #f384be, #b95fa0); box-shadow: 0 0 0 5px rgba(243,132,190,.16);
  }
  .sec.admin .sec-title .dot { background: linear-gradient(140deg, #f0b45f, #d98a1f); box-shadow: 0 0 0 5px rgba(240,180,95,.18); }
  .sec-title .badge {
    margin-left: auto; padding: 3px 12px; border-radius: 99px;
    background: #fff3dc; color: #a9690d; font-size: 15px; font-weight: 700; letter-spacing: .4px;
  }
  .rows { margin-top: 14px; }
  .row { display: flex; align-items: baseline; gap: 18px; padding: 7px 0; }
  .row + .row { border-top: 1px dashed rgba(113,70,111,.16); }
  .row .cmd { flex: 0 0 320px; }
  .row .cmd span {
    display: inline-block; padding: 4px 13px; border-radius: 10px;
    background: linear-gradient(140deg, #ffeaf5, #ffe0ef); color: #c44786;
    font-size: 19px; font-weight: 750; letter-spacing: .45px;
    box-shadow: inset 0 0 0 1px rgba(196,71,134,.14);
  }
  .sec.admin .row .cmd span { background: linear-gradient(140deg, #fff4e2, #ffe9cd); color: #a9690d; box-shadow: inset 0 0 0 1px rgba(169,105,13,.16); }
  .row .desc { flex: 1 1 auto; color: #705276; font-size: 18px; line-height: 1.72; letter-spacing: .35px; }
  .row .desc.only { color: #71466f; font-weight: 600; }
  .foot {
    margin-top: 30px; padding-top: 18px; border-top: 1px solid rgba(113,70,111,.16);
    display: flex; justify-content: space-between; align-items: baseline; gap: 16px;
    color: #6c93a8; font-size: 16px; line-height: 1.7; letter-spacing: .3px;
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
            if command:
                chip = f'<div class="cmd"><span>{escape(command)}</span></div>'
                desc = f'<div class="desc">{escape(description)}</div>'
            else:
                chip = ""
                desc = f'<div class="desc only">{escape(description)}</div>'
            rows.append(f'<div class="row">{chip}{desc}</div>')
        badge = '<span class="badge">仅 AstrBot 管理员</span>' if is_admin else ""
        cls = "sec admin" if is_admin else "sec"
        sections.append(
            f'<div class="{cls}"><div class="sec-title"><i class="dot"></i>'
            f"{escape(title)}{badge}</div>"
            f'<div class="rows">{"".join(rows)}</div></div>'
        )
    sub_bits = []
    if version:
        sub_bits.append(f"版本 v{escape(version)}")
    if group_name:
        sub_bits.append(escape(group_name))
    if requester:
        sub_bits.append(f"请求人 {escape(requester)}")
    subtitle = " · ".join(sub_bits)
    sub_html = f'<div class="sub">{subtitle}</div>' if subtitle else ""
    body = "".join(sections)
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{_STYLE % {'width': CARD_WIDTH}}</style></head><body>"
        '<div class="page">'
        f'<div class="title"><span class="emoji">🛡️</span> {escape(MENU_TITLE)}{sub_html}</div>'
        f'<section class="panel">{body}</section>'
        '<div class="foot">'
        f"<span>{escape(MENU_TIP)}</span>"
        f"<span>{escape(requested_at)}</span>"
        "</div></div></body></html>"
    )
