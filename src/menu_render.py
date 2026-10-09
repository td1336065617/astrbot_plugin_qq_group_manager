"""群管理菜单的图片渲染。

文字版见 `src/commands.py:menu_text()`（两处共用 `MENU_SECTIONS` 数据，不会走偏）。
渲染走 AstrBot 的 `html_render`（远端 t2i 服务），失败由调用方回落到文字。
"""

from __future__ import annotations

from html import escape

from .commands import MENU_SECTIONS, MENU_TIP, MENU_TITLE

#: 卡片宽度（px）：手机端 QQ 预览与桌面端都可读
CARD_WIDTH = 620

_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0e1015; }
  .card {
    width: %(width)dpx; padding: 26px 30px 20px;
    background: linear-gradient(158deg, #191d26 0%%, #12151c 100%%);
    color: #e9ecf3;
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  .head { display: flex; align-items: center; gap: 12px; }
  .head .logo { font-size: 30px; line-height: 1; }
  .head h1 { font-size: 25px; font-weight: 700; letter-spacing: 0.5px; }
  .meta { margin-top: 8px; font-size: 12.5px; color: #8b93a7; }
  .sec { margin-top: 20px; }
  .sec h2 {
    font-size: 14.5px; font-weight: 600; color: #6fb7ff;
    padding-bottom: 7px; border-bottom: 1px solid #262b37;
  }
  .sec.admin h2 { color: #ffc75f; }
  ul { list-style: none; margin-top: 9px; }
  li {
    position: relative; padding: 4px 0 4px 17px;
    font-size: 14.5px; line-height: 1.6; color: #d9dee9;
  }
  li::before {
    content: ""; position: absolute; left: 3px; top: 12px;
    width: 5px; height: 5px; border-radius: 50%%; background: #3f8cff;
  }
  .sec.admin li::before { background: #ffb43f; }
  .foot {
    margin-top: 22px; padding-top: 12px; border-top: 1px solid #262b37;
    font-size: 12.5px; color: #8b93a7;
  }
"""


def menu_html(version: str = "", requested_at: str = "", requester: str = "") -> str:
    """把菜单数据渲染成一张深色卡片的 HTML（交给 t2i 服务出图）。"""
    sections = []
    for title, items in MENU_SECTIONS:
        cls = "sec admin" if title.startswith("AstrBot") else "sec"
        rows = "".join(f"<li>{escape(item)}</li>" for item in items)
        sections.append(f'<div class="{cls}"><h2>{escape(title)}</h2><ul>{rows}</ul></div>')
    meta_bits = [f"v{escape(version)}" if version else ""]
    if requested_at:
        meta_bits.append(escape(requested_at))
    if requester:
        meta_bits.append(f"请求人 {escape(requester)}")
    meta = " · ".join(bit for bit in meta_bits if bit)
    body = "".join(sections)
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{_STYLE % {'width': CARD_WIDTH}}</style></head><body>"
        '<div class="card">'
        '<div class="head"><span class="logo">🛡️</span>'
        f"<h1>{escape(MENU_TITLE)}</h1></div>"
        f'<div class="meta">{meta}</div>'
        f"{body}"
        f'<div class="foot">{escape(MENU_TIP)}</div>'
        "</div></body></html>"
    )
