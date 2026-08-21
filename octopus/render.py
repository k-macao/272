"""推送 HTML 渲染 —— 电子杂志 × 电子墨水风格。

微信内置浏览器会剥掉 <style> 标签，所有样式必须写成内联 style，
且避免用 flex/grid 这类支持不稳的布局，一律用 table/div + 内联属性。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from .models import Item, SourceResult, TimeQuality
from .timeutil import humanize, stamp

# --- 配色 ------------------------------------------------------------------
BG = "#d2d5d8"          # 深浅灰主背景
CARD_BG = "#eceef0"     # 浅灰卡片底
NAVY = "#111111"        # 正文主色：黑
NAVY_DEEP = "#090909"   # 标题黑
NAVY_SOFT = "#5b5f64"   # 次要信息灰
BORDER = "#a4a9ae"
ACCENT = "#b7ff26"      # 荧光绿
ACCENT_WASH = "#edf8d1" # 荧光绿浅点缀
SURFACE_ALT = "#d9dde0" # 灰色辅助底
CODE_BG = "#181a1d"
CODE_TEXT = "#eff6df"
QUOTE_BG = "#e1e4e7"
WARN_BG = "#e7e1de"
WARN_BORDER = "#c9beb9"
WARN_TEXT = "#695149"
RED = "#a63a2b"         # 风险/警示
GREEN = "#2c6b4f"

MANUAL_TITLE = "章鱼 AI 全景分析"
MANUAL_SUBTITLE = "全网 AI 调研境内境外数据，由多个大模型混合部署。"
MANUAL_FOOTER_AUTHOR = "作者：章鱼 ai      仅供参考，分析研究"
MANUAL_FOOTER_NOTE = (
    "全网境内外为你寻找蛛丝马迹-提供全景视野分析，由多模型协同推理决策，"
    "底层所使用的大语言模型（LLM）多模式背后结合使用了多种不同的先进模型，"
    "包括但不限于 Claude、ChatGPT、Gemini、Grok、Qwen 以及 Kimi。"
    "根据不同的资产管理任务需求，更好地发挥各个模型的优势来提供数据支持！[加油]"
)

TIME_BADGE = {
    TimeQuality.EXACT: ("准确", ACCENT),
    TimeQuality.DERIVED: ("推算", NAVY_SOFT),
    TimeQuality.DATE: ("当日", "#7a766d"),
}

# 每张顶层卡片之间插入一个不可见标记。PushPlus 正文过长时，通知层只在
# 这些边界分页，绝不再从一段 HTML 的中间硬截断（硬截断会让微信正文样式错乱）。
HTML_BLOCK_SEPARATOR = "<!--octopus:block-->"


def _document(cards: list[str]) -> str:
    """把顶层卡片拼成完整正文，并保留安全分页边界。"""
    outer = (
        f'<div style="background:{BG};padding:12px 10px;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\','
        f'\'Helvetica Neue\',Helvetica,Arial,sans-serif;color:{NAVY};'
        f'line-height:1.72;font-size:13px;word-break:break-word;">'
    )
    return outer + HTML_BLOCK_SEPARATOR.join(cards) + "</div>"


def render_html(
    groups: list[tuple[SourceResult, list[Item]]],
    *,
    total: int,
    window_minutes: int,
    ref: datetime,
    failures: list[SourceResult],
    degraded: list[SourceResult],
) -> str:
    """整合所有源的条目，输出一封完整的推送正文。"""
    cards: list[str] = [_header(total, window_minutes, ref)]

    if total == 0:
        cards.append(_empty_card(window_minutes))
    else:
        for result, items in groups:
            if items:
                cards.append(_section(result, items, ref))

    cards.append(_footer(ref, failures, degraded, window_minutes))
    return _document(cards)


# ---------------------------------------------------------------------------
def _header(total: int, window_minutes: int, ref: datetime) -> str:
    window_text = _window_text(window_minutes)
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:5px solid {ACCENT};border-radius:8px;padding:12px 14px;'
        f'margin-bottom:12px;">'
        f'<div style="font-size:19px;font-weight:700;color:{NAVY_DEEP};'
        f'letter-spacing:.5px;">章鱼 AI · A股情报速递</div>'
        f'<div style="font-size:13px;color:{NAVY_SOFT};margin-top:6px;">'
        f'扫描时间 {stamp(ref)}（北京时间）</div>'
        f'<div style="font-size:13px;color:{NAVY_SOFT};margin-top:3px;">'
        f'本轮新增 <b style="color:{ACCENT};font-size:15px;">{total}</b> 条'
        f' · 时间窗口 {window_text} · 全部条目已校验发布时间</div>'
        f"</div>"
    )


def _section(result: SourceResult, items: list[Item], ref: datetime) -> str:
    rows = "".join(_row(item, ref) for item in items)
    degraded_note = ""
    if result.degraded:
        degraded_note = (
            f'<span style="font-size:11px;color:{NAVY_SOFT};font-weight:400;">'
            f"（{html.escape(result.degraded)}）</span>"
        )
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;margin-bottom:12px;">'
        f'<div style="font-size:15px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:8px;border-bottom:2px solid {BORDER};">'
        f"▍{html.escape(result.source_label)}"
        f'<span style="font-size:12px;color:{NAVY_SOFT};font-weight:400;">'
        f" · {len(items)} 条</span>{degraded_note}</div>"
        f"{rows}</div>"
    )


def _row(item: Item, ref: datetime) -> str:
    title = html.escape(item.title)
    if item.url:
        title_html = (
            f'<a href="{html.escape(item.url, quote=True)}" '
            f'style="color:{NAVY_DEEP};text-decoration:none;font-weight:600;">{title}</a>'
        )
    else:
        title_html = f'<span style="color:{NAVY_DEEP};font-weight:600;">{title}</span>'

    when = humanize(item.published_at, ref=ref) if item.published_at else "—"
    exact = f"{item.published_at:%m-%d %H:%M}" if item.published_at else ""
    badge_text, badge_color = TIME_BADGE.get(item.time_quality, ("", NAVY_SOFT))

    meta = (
        f'<span style="color:{ACCENT};font-weight:600;">{when}</span>'
        f'<span style="color:{NAVY_SOFT};"> · {exact}</span>'
    )
    if badge_text and item.time_quality is not TimeQuality.EXACT:
        meta += (
            f'<span style="color:{badge_color};border:1px solid {BORDER};'
            f'border-radius:3px;padding:0 4px;margin-left:5px;font-size:11px;">'
            f"{badge_text}</span>"
        )

    tags_html = ""
    if item.tags:
        chips = "".join(
            f'<span style="display:inline-block;background:{ACCENT_WASH};color:{ACCENT};'
            f'border-radius:3px;padding:1px 6px;margin:0 4px 0 0;font-size:11px;">'
            f"{html.escape(str(tag))}</span>"
            for tag in item.tags[:3]
        )
        tags_html = f'<div style="margin-top:4px;">{chips}</div>'

    summary_html = ""
    if item.summary:
        summary_html = (
            f'<div style="font-size:13px;color:{NAVY};opacity:.85;margin-top:4px;">'
            f"{html.escape(item.summary)}</div>"
        )

    return (
        f'<div style="padding:8px 0;border-bottom:1px dashed {BORDER};">'
        f'<div style="font-size:14px;">{title_html}</div>'
        f"{summary_html}"
        f'<div style="font-size:12px;margin-top:4px;">{meta}</div>'
        f"{tags_html}"
        f"</div>"
    )


def _empty_card(window_minutes: int) -> str:
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:16px 14px;margin-bottom:12px;text-align:center;">'
        f'<div style="font-size:15px;color:{NAVY_DEEP};font-weight:600;">'
        f"本轮无新增内容</div>"
        f'<div style="font-size:13px;color:{NAVY_SOFT};margin-top:6px;">'
        f"十个源均已扫描，{_window_text(window_minutes)}内没有未推送过的新条目。"
        f"抓取程序运行正常。</div></div>"
    )


def _footer(
    ref: datetime,
    failures: list[SourceResult],
    degraded: list[SourceResult],
    window_minutes: int,
) -> str:
    lines: list[str] = []
    lines.append(
        f"数据源：问财 · 巨潮 · 迈博汇金 · 集思录 · 证券之星 · "
        f"慧博投研 · 国家统计局 · iFinD · 东方财富 · 萝卜投研"
    )
    if degraded:
        notes = "；".join(f"{r.source_label}{r.degraded}" for r in degraded if r.degraded)
        if notes:
            lines.append(f"降级说明：{notes}")
    if failures:
        names = "、".join(r.source_label for r in failures)
        lines.append(f"本轮未取到数据：{names}（已自动重试，下轮继续）")
    lines.append("时间校验：仅推送带可验证发布时间的条目，无时间戳或时间异常的一律丢弃")
    lines.append("内容由程序自动抓取整合，仅供参考，不构成投资建议")

    body = "".join(
        f'<div style="margin-top:3px;">{html.escape(line)}</div>' for line in lines
    )
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;font-size:11px;color:{NAVY_SOFT};">'
        f'<div style="font-weight:600;color:{NAVY};margin-bottom:4px;">章鱼 AI</div>'
        f"{body}</div>"
    )


def _window_text(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def render_title(total: int, ref: datetime, top: Item | None) -> str:
    """推送标题：微信通知栏只显示这一行，要一眼看出有没有料。"""
    base = f"章鱼AI {ref:%m-%d %H:%M}"
    if total == 0:
        return f"{base} · 本轮无新增"
    if top is not None:
        headline = top.title.strip()
        if len(headline) > 22:
            headline = headline[:22] + "…"
        return f"{base} · {total}条 · {headline}"
    return f"{base} · {total}条新情报"


# ---------------------------------------------------------------------------
# 手动主题分析推送：人工录入 AI 分析内容，直接渲染成一条独立推送。
# 与抓取推送共用同一套浅灰底 + 深蓝字样式，但不经过时间校验与去重。
# ---------------------------------------------------------------------------


def render_manual(
    topic: str,
    content: str,
    *,
    ref: datetime,
    ai_summary: str = "",
    ai_model: str = "DeepSeek-V4",
    markdown: bool | None = None,
) -> str:
    """渲染人工录入内容。

    ``markdown=None`` 时自动识别标题、列表、表格和代码块。普通多行文本仍按
    原样换行展示，避免把聊天式输入误判成 Markdown。
    """
    topic = (topic or "").strip()
    content = (content or "").strip()
    ai_summary = (ai_summary or "").strip()
    if markdown is None:
        markdown = _looks_like_markdown(content)

    cards: list[str] = [_manual_header(topic, ai_model=ai_model if ai_summary else "")]
    if ai_summary:
        cards.append(_manual_ai_card(ai_summary, ai_model))

    body_title = topic or ("原始录入内容" if ai_summary else "正文")
    if markdown:
        cards.extend(_markdown_cards(body_title, content))
    else:
        cards.append(_manual_card(body_title, content))
    cards.append(_manual_footer())
    return _document(cards)


def _manual_header(topic: str, ai_model: str = "") -> str:
    topic_html = ""
    if topic:
        topic_html = (
            f'<div style="margin-top:10px;background:{SURFACE_ALT};border:1px solid {BORDER};'
            f'border-left:3px solid {ACCENT};border-radius:6px;padding:8px 9px;">'
            f'<div style="font-size:10px;letter-spacing:.8px;color:{NAVY_SOFT};">本期主题</div>'
            f'<div style="font-size:13px;font-weight:700;color:{NAVY_DEEP};margin-top:3px;">'
            f"{html.escape(topic)}</div></div>"
        )
    model_html = (
        f'<div style="font-size:11px;color:{NAVY_SOFT};margin-top:8px;">'
        f"模型协同摘要：{html.escape(ai_model)}</div>"
        if ai_model
        else ""
    )
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:6px solid {ACCENT};border-radius:8px;padding:13px 14px;'
        f'margin-bottom:12px;">'
        f'<div style="display:inline-block;background:{ACCENT_WASH};color:{NAVY_DEEP};'
        f'border:1px solid {ACCENT};padding:4px 10px;border-radius:4px;'
        f'font-size:20px;font-weight:800;letter-spacing:.7px;">'
        f"{MANUAL_TITLE}</div>"
        f'<div style="height:3px;width:56px;background:{ACCENT};margin-top:8px;border-radius:3px;"></div>'
        f'<div style="font-size:12px;color:{NAVY};margin-top:8px;line-height:1.7;">'
        f"{MANUAL_SUBTITLE}</div>"
        f"{model_html}{topic_html}</div>"
    )


def _manual_ai_card(ai_summary: str, ai_model: str) -> str:
    title = f"✨ DeepSeek AI 智能提炼 · 模型协同摘要 ({html.escape(ai_model)})"
    body = _rich_text(ai_summary)
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:4px solid {ACCENT};border-radius:8px;padding:11px 12px;margin-bottom:12px;">'
        f'<div style="font-size:14px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:8px;border-bottom:1px solid {BORDER};">'
        f"▍{title}</div>"
        f'<div style="font-size:13px;color:{NAVY};line-height:1.8;">{body}</div>'
        f"</div>"
    )


def _manual_card(topic: str, content: str) -> str:
    title = html.escape(topic) if topic else "正文"
    body = html.escape(content).replace("\n", "<br>")
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:11px 12px;margin-bottom:12px;">'
        f'<div style="font-size:14px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:9px;border-bottom:1px solid {BORDER};">'
        f"▍{title}</div>"
        f'<div style="font-size:13px;color:{NAVY};line-height:1.82;">{body}</div>'
        f"</div>"
    )


def _manual_footer() -> str:
    return (
        f'<div style="background:{SURFACE_ALT};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;font-size:11px;color:{NAVY};">'
        f'<div style="font-weight:700;color:{NAVY_DEEP};margin-bottom:6px;">{MANUAL_FOOTER_AUTHOR}</div>'
        f'<div style="line-height:1.78;">{MANUAL_FOOTER_NOTE}</div>'
        f"</div>"
    )


# ---------------------------------------------------------------------------
# 合并研报与轻量 Markdown 渲染
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MarkdownBlock:
    kind: str
    rendered: str
    text: str = ""
    level: int = 0


_MARKDOWN_HINT = re.compile(
    r"(?m)^\s*(?:#{1,6}\s+|```|~~~|>\s*|[-+*]\s+|\d+[.)]\s+|(?:---+|___+|\*\*\*+)\s*$)"
)
_TABLE_DIVIDER = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_LIST_ITEM = re.compile(r"^(\s*)([-+*]|\d+[.)])\s+(.+)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w.+-]*)\s*$")


def _looks_like_markdown(text: str) -> bool:
    """保守识别 Markdown；只有结构标记明确时才启用富文本渲染。"""
    if not text:
        return False
    if _MARKDOWN_HINT.search(text):
        return True
    lines = text.splitlines()
    return any(
        i + 1 < len(lines) and "|" in line and _TABLE_DIVIDER.match(lines[i + 1])
        for i, line in enumerate(lines)
    )


def _inline_markdown(value: str) -> str:
    """安全渲染少量行内 Markdown（链接、代码、粗体、删除线）。"""
    escaped = html.escape(value.strip())
    tokens: dict[str, str] = {}

    def protect(fragment: str) -> str:
        key = f"\ue000{len(tokens)}\ue001"
        tokens[key] = fragment
        return key

    escaped = re.sub(
        r"`([^`\n]+)`",
        lambda m: protect(
            f'<code style="background:{SURFACE_ALT};color:{NAVY_DEEP};border-radius:3px;'
            f'padding:1px 4px;font-family:Menlo,Consolas,monospace;font-size:12px;">'
            f"{m.group(1)}</code>"
        ),
        escaped,
    )

    def link(match: re.Match[str]) -> str:
        label, raw_url = match.group(1), html.unescape(match.group(2)).strip()
        parsed = urlsplit(raw_url)
        if parsed.scheme not in ("http", "https"):
            # 微信中的文内锚点并不可靠；保留可读标签，不制造无效链接。
            return label
        url = html.escape(raw_url, quote=True)
        return protect(
            f'<a href="{url}" style="color:{ACCENT};text-decoration:none;">{label}</a>'
        )

    escaped = re.sub(r"\[([^\]]+)]\(([^)\s]+)(?:\s+[^)]*)?\)", link, escaped)
    escaped = re.sub(
        r"\*\*(.+?)\*\*|__(.+?)__",
        lambda m: f'<strong style="color:{NAVY_DEEP};">{m.group(1) or m.group(2)}</strong>',
        escaped,
    )
    escaped = re.sub(
        r"~~(.+?)~~",
        r'<span style="text-decoration:line-through;opacity:.7;">\1</span>',
        escaped,
    )
    for key, fragment in tokens.items():
        escaped = escaped.replace(key, fragment)
    return escaped


def _split_table_row(line: str) -> list[str]:
    """拆 Markdown 表格行，兼容反斜线转义的竖线。"""
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]
    sentinel = "\ue100"
    value = value.replace(r"\|", sentinel)
    return [cell.strip().replace(sentinel, "|") for cell in value.split("|")]


def _render_table(rows: list[list[str]]) -> str:
    width = max((len(r) for r in rows), default=1)
    normalized = [r + [""] * (width - len(r)) for r in rows]
    min_width = min(760, max(300, width * 125))
    head = "".join(
        f'<th style="background:{ACCENT_WASH};color:{NAVY_DEEP};font-weight:700;'
        f'padding:6px;border:1px solid {BORDER};text-align:left;vertical-align:top;">'
        f"{_inline_markdown(cell)}</th>"
        for cell in normalized[0]
    )
    body_rows: list[str] = []
    for row_index, row in enumerate(normalized[1:]):
        bg = CARD_BG if row_index % 2 == 0 else SURFACE_ALT
        cells = "".join(
            f'<td style="background:{bg};padding:6px;border:1px solid {BORDER};'
            f'vertical-align:top;">{_inline_markdown(cell)}</td>'
            for cell in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f'<div style="overflow-x:auto;margin:9px 0;">'
        f'<table style="width:100%;min-width:{min_width}px;border-collapse:collapse;'
        f'font-size:12px;line-height:1.5;"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _markdown_blocks(text: str) -> list[_MarkdownBlock]:
    """把常见研报 Markdown 转成微信兼容的内联样式块。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_MarkdownBlock] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            marker, language = fence.groups()
            i += 1
            code: list[str] = []
            while i < len(lines) and not re.match(rf"^\s*{re.escape(marker)}\s*$", lines[i]):
                code.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            label = (
                f'<div style="font-size:10px;color:{NAVY_SOFT};margin-bottom:5px;">'
                f"{html.escape(language)}</div>"
                if language
                else ""
            )
            rendered = (
                f'<div style="margin:9px 0;background:{NAVY_DEEP};border-radius:6px;'
                f'padding:9px 10px;color:{CODE_TEXT};">{label}'
                f'<pre style="margin:0;white-space:pre-wrap;word-break:break-word;'
                f'font:11px/1.55 Menlo,Consolas,monospace;">'
                f"{html.escape(chr(10).join(code))}</pre></div>"
            )
            blocks.append(_MarkdownBlock("code", rendered))
            continue

        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            title = re.sub(r"\s+#+$", "", heading.group(2)).strip()
            if level <= 2:
                blocks.append(_MarkdownBlock("heading", "", title, level))
            else:
                size = 15 if level == 3 else 14
                rendered = (
                    f'<div style="font-size:{size}px;font-weight:700;color:{NAVY_DEEP};'
                    f'margin:14px 0 6px;padding-left:8px;border-left:3px solid {ACCENT};">'
                    f"{_inline_markdown(title)}</div>"
                )
                blocks.append(_MarkdownBlock("subheading", rendered, title, level))
            i += 1
            continue

        if i + 1 < len(lines) and "|" in line and _TABLE_DIVIDER.match(lines[i + 1]):
            rows = [_split_table_row(line)]
            i += 2  # 跳过表头分隔行
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            blocks.append(_MarkdownBlock("table", _render_table(rows)))
            continue

        if re.match(r"^\s*(?:---+|___+|\*\*\*+)\s*$", line):
            # 卡片本身已经承担章节分隔，不再叠加一排横线。
            i += 1
            continue

        if stripped.startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            rendered = (
                f'<div style="background:{QUOTE_BG};border-left:4px solid {NAVY_SOFT};'
                f'border-radius:0 5px 5px 0;padding:7px 9px;margin:8px 0;'
                f'font-size:12px;color:{NAVY_SOFT};">'
                + "<br>".join(_inline_markdown(q) for q in quote)
                + "</div>"
            )
            blocks.append(_MarkdownBlock("quote", rendered))
            continue

        list_match = _LIST_ITEM.match(line)
        if list_match:
            items: list[str] = []
            while i < len(lines):
                match = _LIST_ITEM.match(lines[i])
                if not match:
                    break
                indent, marker, item = match.groups()
                symbol = marker if marker[0].isdigit() else "•"
                left = 8 + min(24, len(indent) * 4)
                items.append(
                    f'<div style="padding:3px 0 3px {left}px;">'
                    f'<span style="display:inline-block;width:24px;margin-left:-24px;'
                    f'color:{ACCENT};font-weight:700;">{html.escape(symbol)}</span>'
                    f'<span>{_inline_markdown(item)}</span></div>'
                )
                i += 1
            blocks.append(
                _MarkdownBlock(
                    "list",
                    f'<div style="margin:6px 0;font-size:14px;">{"".join(items)}</div>',
                )
            )
            continue

        paragraph = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip():
            candidate = lines[i]
            if (
                _FENCE.match(candidate)
                or _HEADING.match(candidate.strip())
                or _LIST_ITEM.match(candidate)
                or candidate.strip().startswith(">")
                or re.match(r"^\s*(?:---+|___+|\*\*\*+)\s*$", candidate)
                or (
                    i + 1 < len(lines)
                    and "|" in candidate
                    and _TABLE_DIVIDER.match(lines[i + 1])
                )
            ):
                break
            paragraph.append(candidate.strip())
            i += 1
        value = "<br>".join(_inline_markdown(part) for part in paragraph)
        # 大模型常用【模块名】作行首标题，单独强调，避免所有文字挤成一团。
        value = re.sub(
            r"^【([^】]+)】\s*",
            rf'<strong style="color:{ACCENT};">【\1】</strong> ',
            value,
        )
        blocks.append(
            _MarkdownBlock(
                "paragraph",
                f'<div style="font-size:14px;color:{NAVY};margin:7px 0;line-height:1.75;">'
                f"{value}</div>",
            )
        )
    return blocks


def _markdown_card(title: str, body: str, *, continued: bool = False) -> str:
    suffix = " · 续" if continued else ""
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:11px 12px;margin-bottom:10px;">'
        f'<div style="font-size:14px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:8px;border-bottom:1px solid {BORDER};">'
        f"▍{_inline_markdown(title)}{suffix}</div>{body}</div>"
    )


def _markdown_cards(
    fallback_title: str,
    content: str,
    *,
    skip_sections: set[str] | None = None,
    drop_preamble: bool = False,
) -> list[str]:
    """按一/二级标题拆卡片；超长章节再按块拆分，保证手机阅读节奏。"""
    skip_sections = {s.strip() for s in (skip_sections or set())}
    blocks = _markdown_blocks(content)
    cards: list[str] = []
    current_title = fallback_title or "正文"
    current: list[str] = []
    current_size = 0
    continuation = False
    skipping = False
    first_heading = True
    section_started = not drop_preamble
    max_card_chars = 16000

    def flush() -> None:
        nonlocal current, current_size, continuation
        if current:
            cards.append(_markdown_card(current_title, "".join(current), continued=continuation))
            current = []
            current_size = 0
            continuation = True

    for block in blocks:
        if block.kind == "heading":
            heading_text = re.sub(r"\s+", "", block.text).casefold()
            fallback_text = re.sub(r"\s+", "", fallback_title).casefold()
            # 顶部 H1 通常与推送标题重复，保留内容但不再显示一次。
            if first_heading and block.level == 1 and heading_text == fallback_text:
                first_heading = False
                continue
            first_heading = False
            flush()
            current_title = block.text or fallback_title or "正文"
            continuation = False
            skipping = block.text.strip() in skip_sections
            section_started = True
            continue
        first_heading = False
        if skipping or not section_started:
            continue
        if current and current_size + len(block.rendered) > max_card_chars:
            flush()
        current.append(block.rendered)
        current_size += len(block.rendered)
    flush()
    return cards or [_manual_card(fallback_title, content)]


def _push_report_markdown(content: str) -> str:
    """只保留分析、数据与结论，去掉合并元数据和实现过程。"""
    skip_titles = {"目录", "合并说明与来源追溯"}
    skip_title_terms = (
        "免费开源金融数据库",
        "因子库构建与数学公式",
        "数据清洗与特征工程",
        "Prompt 架构",
        "投研检查清单",
    )
    skip_line_terms = (
        "免费开源金融数据库",
        "多因子库设计",
        "数据预处理与 A 股特征工程",
        "防空泛 AI 研报 Prompt",
        "方法论层",
        "接入规范与代码实现",
    )
    skipping = False
    in_fence = False
    cleaned: list[str] = []
    for line in (content or "").splitlines():
        stripped = line.strip()
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING.match(stripped)
        if heading and len(heading.group(1)) <= 2:
            title = heading.group(2).strip()
            if title in skip_titles or any(term in title for term in skip_title_terms):
                skipping = True
                continue
            skipping = False
            source = re.match(r"原始报告\s*\d+\s*[：:]\s*(.+)", title)
            if source:
                line = f"## {source.group(1).strip()}"
        if skipping:
            continue
        if stripped.startswith("> **合并时间**") or stripped.startswith("> **文件名**"):
            continue
        if any(term in line for term in skip_line_terms):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def render_merge(
    topic: str,
    content: str,
    *,
    ref: datetime,
    source_count: int = 0,
    ai_summary: str = "",
    ai_model: str = "DeepSeek-V4",
) -> str:
    """把合并后的 Markdown 渲染成适合微信窄屏阅读的章节卡片。"""
    topic = (topic or "合并研报").strip()
    content = _push_report_markdown(content)
    ai_summary = (ai_summary or "").strip()
    cards = [
        (
            f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
            f'border-left:5px solid {ACCENT};border-radius:8px;padding:12px 14px;'
            f'margin-bottom:10px;">'
            f'<div style="font-size:19px;font-weight:700;color:{NAVY_DEEP};">'
            f"章鱼 AI · 合并研报</div>"
            f'<div style="font-size:16px;font-weight:600;color:{ACCENT};margin-top:6px;">'
            f"{html.escape(topic)}</div>"
            f'<div style="font-size:12px;color:{NAVY_SOFT};margin-top:6px;">'
            f"{stamp(ref)}（北京时间）</div></div>"
        )
    ]
    if ai_summary:
        cards.append(_manual_ai_card(ai_summary, ai_model))
    cards.extend(_markdown_cards(topic, content))
    cards.append(
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};border-radius:8px;'
        f'padding:9px 11px;font-size:11px;color:{NAVY_SOFT};">'
        f"仅供研究参考，不构成投资建议</div>"
    )
    return _document(cards)


def render_merge_title(topic: str, ref: datetime, source_count: int = 0) -> str:
    topic = (topic or "合并研报").strip()
    if len(topic) > 18:
        topic = topic[:18] + "…"
    return f"章鱼AI {ref:%m-%d %H:%M} · 合并研报 · {topic}"


def render_manual_title(topic: str, ref: datetime) -> str:
    """手动主题分析的推送标题：按要求去掉时间，统一使用品牌标题。"""
    return MANUAL_TITLE


# ---------------------------------------------------------------------------
# 主题因子分析推送：输入主题 -> 监管 + qlib 因子模型 -> AI 报告
# 沿用同一套浅灰底 + 深蓝字样式，结构上分为：
#   概览卡（主题/板块/数据日期）→ 因子雷达（维度评分表）→ AI 解读
#   → 监管视角 → 数据溯源与合规声明
# ---------------------------------------------------------------------------

#: 因子维度评分的配色档位
_SCORE_COLORS = (
    (70.0, "#b3261e"),   # A股红涨：高分用红
    (55.0, "#c2681b"),
    (45.0, "#5a6f8c"),
    (30.0, "#2c6b4f"),
    (0.0, "#1b5e20"),    # 低分绿
)


def _score_color(score: float | None) -> str:
    if score is None:
        return NAVY_SOFT
    for threshold, color in _SCORE_COLORS:
        if score >= threshold:
            return color
    return NAVY_SOFT


def render_theme(analysis, *, ref: datetime | None = None) -> str:
    """把一次主题因子分析渲染成推送正文。

    analysis 是 octopus.factor.pipeline.ThemeAnalysis；这里只读它的字段，
    不做任何计算 —— 渲染层保持哑管道，方便单测直接构造假对象。
    """
    ref = ref or analysis.ref
    cards: list[str] = [_theme_header(analysis, ref), _theme_overview(analysis)]
    if analysis.all_profiles:
        cards.append(_theme_factor_card(analysis))
    cards.extend(
        [
            _theme_ai_card(analysis),
            _theme_supervision_card(analysis),
            _theme_provenance_card(analysis),
            _theme_disclaimer_card(analysis),
        ]
    )
    return _document(cards)


def _theme_header(analysis, ref: datetime) -> str:
    topic = html.escape(analysis.topic or "未指定主题")
    engine = "DeepSeek 大模型解读" if analysis.used_ai else "内置规则化解读"
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:5px solid {ACCENT};border-radius:8px;padding:12px 14px;'
        f'margin-bottom:12px;">'
        f'<div style="font-size:19px;font-weight:700;color:{NAVY_DEEP};'
        f'letter-spacing:.5px;">章鱼 AI · 主题因子分析</div>'
        f'<div style="font-size:16px;font-weight:600;color:{ACCENT};margin-top:6px;">'
        f"{topic}</div>"
        f'<div style="font-size:12px;color:{NAVY_SOFT};margin-top:6px;">'
        f"A股市场监督管理视角 · qlib Alpha158 因子模型 · {engine}</div>"
        f'<div style="font-size:12px;color:{NAVY_SOFT};margin-top:3px;">'
        f"生成时间 {stamp(ref)}（北京时间）</div>"
        f"</div>"
    )


def _theme_overview(analysis) -> str:
    """概览卡：板块、标的口径、行情日期、监管风险等级。"""
    from .factor.market import data_freshness

    rows: list[tuple[str, str]] = []
    board = analysis.market.board
    if board:
        rows.append(
            (
                "命中板块",
                f"{board.name}（{board.kind}）{board.change:+.2f}%"
                + ("（推算）" if board.change_derived else "")
                + (f" · 主力{board.main_inflow / 1e8:+.2f}亿" if board.main_inflow else ""),
            )
        )
    else:
        rows.append(("命中板块", "未匹配到具体板块，按全市场口径分析"))

    stock_names = "、".join(p.name for p in analysis.profiles) or "—"
    rows.append((f"分析标的（{len(analysis.profiles)}）", stock_names))
    if analysis.benchmark_profiles:
        rows.append(
            ("基准指数", "、".join(p.name for p in analysis.benchmark_profiles))
        )
    rows.append(("行情截至", data_freshness(analysis.data_date, ref=analysis.ref)))

    level = analysis.supervision.risk_level
    level_color = {"高": RED, "中": "#c2681b", "偏低": NAVY_SOFT}.get(level, GREEN)
    sup = analysis.supervision
    rows.append(
        (
            "监管风险",
            f'<span style="color:{level_color};font-weight:700;">{level}</span>'
            f'<span style="color:{NAVY_SOFT};"> · 标的相关 {len(sup.focus)} 条 / '
            f"全市场 {len(sup.events)} 条</span>",
        )
    )

    body = "".join(
        f'<div style="padding:4px 0;font-size:13px;">'
        f'<span style="color:{NAVY_SOFT};display:inline-block;min-width:84px;">'
        f"{html.escape(label)}</span>"
        f'<span style="color:{NAVY};">{value}</span></div>'
        for label, value in rows
    )
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;margin-bottom:12px;">{body}</div>'
    )


def _theme_factor_card(analysis) -> str:
    """因子评分卡：每个标的一张六维评分表。"""
    blocks: list[str] = []
    for profile in analysis.all_profiles:
        if not profile.dimensions:
            blocks.append(
                f'<div style="padding:8px 0;border-bottom:1px dashed {BORDER};'
                f'font-size:13px;color:{NAVY_SOFT};">'
                f"{html.escape(profile.name)}：历史行情不足，未计算因子</div>"
            )
            continue

        composite = profile.composite
        comp_text = "—" if composite is None else f"{composite:.1f}"
        comp_color = _score_color(composite)
        # 用 table 排标题行：微信端 float 支持不稳，两列表格最稳妥，
        # 也避免"名称代码分数"挤在一起连成一串数字。
        head = (
            f'<table style="width:100%;border-collapse:collapse;margin:8px 0 2px;"><tr>'
            f'<td style="padding:0;vertical-align:bottom;">'
            f'<span style="font-size:14px;font-weight:700;color:{NAVY_DEEP};">'
            f"{html.escape(profile.name)}</span>"
            f'<span style="font-size:11px;color:{NAVY_SOFT};">'
            f"&nbsp;{html.escape(profile.code)}</span></td>"
            f'<td style="padding:0;text-align:right;vertical-align:bottom;'
            f'white-space:nowrap;">'
            f'<span style="font-size:16px;font-weight:700;color:{comp_color};">{comp_text}</span>'
            f'<span style="font-size:11px;color:{NAVY_SOFT};">/100</span></td>'
            f"</tr></table>"
            f'<div style="font-size:12px;color:{NAVY_SOFT};margin-bottom:6px;">'
            f"{html.escape(profile.stance)}</div>"
        )

        bars: list[str] = []
        for dim in profile.dimensions:
            score = dim.score
            width = 0 if score is None else max(2, min(100, int(score)))
            color = _score_color(score)
            score_text = "—" if score is None else f"{score:.0f}"
            bars.append(
                f'<div style="margin:5px 0;">'
                f'<div style="font-size:12px;color:{NAVY};">'
                f'<span style="display:inline-block;min-width:66px;">{html.escape(dim.label)}</span>'
                f'<span style="color:{color};font-weight:600;">{score_text}</span>'
                f'<span style="color:{NAVY_SOFT};"> · {html.escape(dim.level)}</span></div>'
                f'<div style="background:{ACCENT_WASH};border-radius:3px;height:6px;margin-top:3px;">'
                f'<div style="background:{color};width:{width}%;height:6px;border-radius:3px;">'
                f"</div></div>"
                f'<div style="font-size:11px;color:{NAVY_SOFT};margin-top:3px;">'
                f"{html.escape(dim.detail)}</div>"
                f"</div>"
            )
        blocks.append(
            f'<div style="padding:6px 0;border-bottom:1px dashed {BORDER};">'
            f"{head}{''.join(bars)}</div>"
        )

    ranking = analysis.ranking()
    rank_html = ""
    if len(ranking) > 1:
        chips = "".join(
            f'<span style="display:inline-block;background:{ACCENT_WASH};'
            f'color:{_score_color(score)};border-radius:3px;padding:1px 6px;'
            f'margin:2px 4px 2px 0;font-size:11px;">'
            f"{html.escape(name)} {score:.0f}</span>"
            for name, score in ranking
        )
        rank_html = (
            f'<div style="margin-top:8px;font-size:11px;color:{NAVY_SOFT};">'
            f"横截面因子分布（仅呈现分布，不构成推荐）</div>"
            f'<div style="margin-top:4px;">{chips}</div>'
        )

    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;margin-bottom:12px;">'
        f'<div style="font-size:15px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:4px;border-bottom:2px solid {BORDER};">'
        f"▍量化因子评分"
        f'<span style="font-size:11px;color:{NAVY_SOFT};font-weight:400;">'
        f" · qlib Alpha158</span></div>"
        f"{''.join(blocks)}{rank_html}</div>"
    )


def _theme_ai_card(analysis) -> str:
    engine = (
        f"✨ DeepSeek AI 解读（{html.escape(analysis.ai_model)}）"
        if analysis.used_ai
        else "🧮 规则化因子解读（未配置大模型 Key）"
    )
    body = _rich_text(analysis.ai_report)
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:4px solid #1b5e20;border-radius:8px;padding:12px 14px;'
        f'margin-bottom:12px;">'
        f'<div style="font-size:16px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:10px;border-bottom:2px solid {BORDER};">'
        f"▍{engine}</div>"
        f'<div style="font-size:14px;color:{NAVY};line-height:1.75;">{body}</div>'
        f"</div>"
    )


def _theme_supervision_card(analysis) -> str:
    sup = analysis.supervision
    level = sup.risk_level
    level_color = {"高": RED, "中": "#c2681b", "偏低": NAVY_SOFT}.get(level, GREEN)

    lines: list[str] = [
        f'<div style="font-size:13px;margin-bottom:6px;">'
        f'<span style="color:{NAVY_SOFT};">整体监管风险：</span>'
        f'<span style="color:{level_color};font-weight:700;">{level}</span>'
        f'<span style="color:{NAVY_SOFT};"> · {html.escape(sup.summary_line())}</span></div>'
    ]

    related = sup.focus
    if related:
        lines.append(
            f'<div style="font-size:12px;font-weight:700;color:{NAVY_DEEP};'
            f'margin:8px 0 4px;">与分析标的直接相关</div>'
        )
        lines.extend(_supervision_row(e) for e in related[:6])
    focus_ids = {id(e) for e in related}
    others = [e for e in sup.events if id(e) not in focus_ids][:6]
    if others:
        lines.append(
            f'<div style="font-size:12px;font-weight:700;color:{NAVY_DEEP};'
            f'margin:8px 0 4px;">同期市场监管动态</div>'
        )
        lines.extend(_supervision_row(e) for e in others)
    if not sup.events:
        lines.append(
            f'<div style="font-size:12px;color:{NAVY_SOFT};">'
            f"近 {sup.window_days} 天未检出与本主题直接相关的监管事件"
            f"（已扫描 {sup.scanned} 条公告）</div>"
        )

    from .factor.supervision import policy_context

    policy = policy_context(analysis.topic)
    if policy:
        lines.append(
            f'<div style="font-size:12px;color:{NAVY_SOFT};margin-top:8px;">'
            f"主题涉及政策敏感词：{html.escape('、'.join(policy))}，"
            f"请以监管部门正式发布口径为准</div>"
        )

    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;margin-bottom:12px;">'
        f'<div style="font-size:15px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:8px;border-bottom:2px solid {BORDER};">'
        f"▍A股市场监督管理</div>"
        f"{''.join(lines)}</div>"
    )


def _supervision_row(event) -> str:
    color = RED if event.severity >= 85 else ("#c2681b" if event.severity >= 65 else NAVY_SOFT)
    title = html.escape(event.title)
    if event.url:
        title = (
            f'<a href="{html.escape(event.url, quote=True)}" '
            f'style="color:{NAVY_DEEP};text-decoration:none;">{title}</a>'
        )
    return (
        f'<div style="padding:5px 0;border-bottom:1px dashed {BORDER};">'
        f'<span style="display:inline-block;background:{ACCENT_WASH};color:{color};'
        f'border-radius:3px;padding:0 5px;margin-right:5px;font-size:11px;">'
        f"{html.escape(event.category)}</span>"
        f'<span style="font-size:13px;">{title}</span>'
        f'<div style="font-size:11px;color:{NAVY_SOFT};margin-top:2px;">'
        f"{event.published_at:%Y-%m-%d %H:%M}</div></div>"
    )


def _theme_provenance_card(analysis) -> str:
    """数据溯源：因子来自哪个 commit、行情截至何时、走了哪些降级路径。"""
    from .factor.market import data_freshness

    lines = [
        f"因子模型：{analysis.model.provenance}",
        f"因子定义：共 {len(analysis.model.factors)} 个，本次计算核心子集",
        f"行情数据：东方财富公开行情接口，截至 {data_freshness(analysis.data_date, ref=analysis.ref)}",
        f"监管数据：东方财富公告中心，窗口 {analysis.supervision.window_days} 天，"
        f"已扫描 {analysis.supervision.scanned} 条公告",
    ]
    if analysis.compliance_result is not None:
        lines.append(analysis.compliance_result.summary())
    lines.extend(analysis.notes)

    body = "".join(
        f'<div style="margin-top:3px;">· {html.escape(line)}</div>' for line in lines
    )
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;margin-bottom:12px;'
        f'font-size:11px;color:{NAVY_SOFT};">'
        f'<div style="font-weight:600;color:{NAVY};margin-bottom:4px;font-size:12px;">'
        f"数据溯源与口径</div>{body}</div>"
    )


def _theme_disclaimer_card(analysis) -> str:
    from .factor.compliance import disclaimer

    body = "".join(
        f'<div style="margin-top:4px;">{html.escape(line)}</div>' for line in disclaimer()
    )
    return (
        f'<div style="background:{WARN_BG};border:1px solid {WARN_BORDER};'
        f'border-radius:8px;padding:10px 12px;font-size:11px;color:{WARN_TEXT};">'
        f'<div style="font-weight:700;color:{RED};margin-bottom:4px;font-size:12px;">'
        f"⚠ 风险提示与免责声明</div>{body}</div>"
    )


_THEME_HEADING = re.compile(r"^【(.+?)】\s*(.*)$")


def _rich_text(text: str) -> str:
    """把大模型/规则化输出的纯文本渲染成带层次的 HTML。

    只做两件事：【小标题】高亮成块级标题，其余按行转 <br>。
    全程 html.escape，杜绝模型输出里夹带标签。
    """
    lines = (text or "").split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append('<div style="height:8px;"></div>')
            continue
        match = _THEME_HEADING.match(stripped)
        if match:
            title = html.escape(match.group(1))
            rest = html.escape(match.group(2))
            out.append(
                f'<div style="font-size:14px;font-weight:700;color:{NAVY_DEEP};'
                f'margin:10px 0 4px;">【{title}】{rest}</div>'
            )
            continue
        out.append(f"<div>{html.escape(stripped)}</div>")
    return "".join(out)


def render_theme_title(topic: str, analysis=None, ref: datetime | None = None) -> str:
    """主题分析推送的标题：主题 + 综合分 + 监管风险，一眼看清。"""
    ref = ref or (analysis.ref if analysis is not None else None)
    base = f"章鱼AI {ref:%m-%d %H:%M} · 因子分析" if ref else "章鱼AI · 因子分析"
    topic = (topic or "").strip() or "主题"
    if len(topic) > 16:
        topic = topic[:16] + "…"

    extra = ""
    if analysis is not None:
        scores = [p.composite for p in analysis.profiles if p.composite is not None]
        if scores:
            extra = f" · 因子{sum(scores) / len(scores):.0f}分"
        level = analysis.supervision.risk_level
        if level in ("高", "中"):
            extra += f" · 监管风险{level}"
    return f"{base} · {topic}{extra}"
