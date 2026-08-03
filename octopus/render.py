"""推送 HTML 渲染 —— 浅灰底色 + 深蓝字体.

微信内置浏览器会剥掉 <style> 标签，所有样式必须写成内联 style，
且避免用 flex/grid 这类支持不稳的布局，一律用 table/div + 内联属性。
"""

from __future__ import annotations

import html
from datetime import datetime

from .models import Item, SourceResult, TimeQuality
from .timeutil import humanize, stamp

# --- 配色 ------------------------------------------------------------------
BG = "#eceff3"          # 浅灰底色
CARD_BG = "#f6f7f9"     # 卡片底色（比背景略浅，形成层次）
NAVY = "#12305c"        # 深蓝主字体
NAVY_DEEP = "#0a1f3d"   # 标题深蓝
NAVY_SOFT = "#3a5a86"   # 次要信息蓝灰
BORDER = "#c9d3e0"
ACCENT = "#1d4f91"      # 强调蓝
RED = "#b3261e"         # 涨/警示（A股红涨）
GREEN = "#1b5e20"

TIME_BADGE = {
    TimeQuality.EXACT: ("准确", "#1d4f91"),
    TimeQuality.DERIVED: ("推算", "#5a6f8c"),
    TimeQuality.DATE: ("当日", "#6b7b93"),
}


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
    parts: list[str] = []
    parts.append(
        f'<div style="background:{BG};padding:14px 12px;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\','
        f'\'Helvetica Neue\',Helvetica,Arial,sans-serif;color:{NAVY};'
        f'line-height:1.6;font-size:15px;">'
    )
    parts.append(_header(total, window_minutes, ref))

    if total == 0:
        parts.append(_empty_card(window_minutes))
    else:
        for result, items in groups:
            if items:
                parts.append(_section(result, items, ref))

    parts.append(_footer(ref, failures, degraded, window_minutes))
    parts.append("</div>")
    return "".join(parts)


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
            f'<span style="display:inline-block;background:#dde5f0;color:{ACCENT};'
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
) -> str:
    """把人工录入的 AI 分析主题/内容（可选搭配 DeepSeek 大模型提炼）渲染成推送正文。"""
    topic = (topic or "").strip()
    content = (content or "").strip()
    ai_summary = (ai_summary or "").strip()
    parts: list[str] = []
    parts.append(
        f'<div style="background:{BG};padding:14px 12px;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\','
        f'\'Helvetica Neue\',Helvetica,Arial,sans-serif;color:{NAVY};'
        f'line-height:1.6;font-size:15px;">'
    )
    parts.append(_manual_header(ref, ai_model=ai_model if ai_summary else ""))
    if ai_summary:
        parts.append(_manual_ai_card(ai_summary, ai_model))
        parts.append(_manual_card(topic or "原始录入内容", content))
    else:
        parts.append(_manual_card(topic, content))
    parts.append(_manual_footer(ref))
    parts.append("</div>")
    return "".join(parts)


def _manual_header(ref: datetime, ai_model: str = "") -> str:
    sub = (
        f"DeepSeek 大模型提炼（{html.escape(ai_model)}） · 人工录入 · 发布时间 {stamp(ref)}（北京时间）"
        if ai_model
        else f"人工录入内容 · 发布时间 {stamp(ref)}（北京时间）"
    )
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:5px solid {ACCENT};border-radius:8px;padding:12px 14px;'
        f'margin-bottom:12px;">'
        f'<div style="font-size:19px;font-weight:700;color:{NAVY_DEEP};'
        f'letter-spacing:.5px;">章鱼 AI · 主题分析</div>'
        f'<div style="font-size:13px;color:{NAVY_SOFT};margin-top:6px;">'
        f"{sub}</div>"
        f"</div>"
    )


def _manual_ai_card(ai_summary: str, ai_model: str) -> str:
    title = f"✨ DeepSeek AI 智能提炼 · 分类与摘要 ({html.escape(ai_model)})"
    body = html.escape(ai_summary).replace("\n", "<br>")
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-left:4px solid #1b5e20;border-radius:8px;padding:12px 14px;margin-bottom:12px;">'
        f'<div style="font-size:16px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:10px;border-bottom:2px solid {BORDER};">'
        f"▍{title}</div>"
        f'<div style="font-size:15px;color:{NAVY};line-height:1.75;">{body}</div>'
        f"</div>"
    )


def _manual_card(topic: str, content: str) -> str:
    title = html.escape(topic) if topic else "AI 分析内容"
    body = html.escape(content).replace("\n", "<br>")
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:12px 14px;margin-bottom:12px;">'
        f'<div style="font-size:16px;font-weight:700;color:{NAVY_DEEP};'
        f'padding-bottom:7px;margin-bottom:10px;border-bottom:2px solid {BORDER};">'
        f"▍{title}</div>"
        f'<div style="font-size:15px;color:{NAVY};line-height:1.75;">{body}</div>'
        f"</div>"
    )


def _manual_footer(ref: datetime) -> str:
    return (
        f'<div style="background:{CARD_BG};border:1px solid {BORDER};'
        f'border-radius:8px;padding:10px 12px;font-size:11px;color:{NAVY_SOFT};">'
        f'<div style="font-weight:600;color:{NAVY};margin-bottom:4px;">章鱼 AI</div>'
        f'<div style="margin-top:3px;">内容由人工录入，未经程序抓取校验</div>'
        f'<div style="margin-top:3px;">仅供参考，不构成投资建议</div>'
        f"</div>"
    )


def render_manual_title(topic: str, ref: datetime) -> str:
    """手动主题分析的推送标题：微信通知栏只显示这一行。"""
    base = f"章鱼AI {ref:%m-%d %H:%M} · AI分析"
    topic = (topic or "").strip()
    if not topic:
        return f"{base} · 主题"
    if len(topic) > 22:
        topic = topic[:22] + "…"
    return f"{base} · {topic}"
