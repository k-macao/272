"""推送 HTML 渲染 —— 浅灰底色 + 深蓝字体.

微信内置浏览器会剥掉 <style> 标签，所有样式必须写成内联 style，
且避免用 flex/grid 这类支持不稳的布局，一律用 table/div + 内联属性。
"""

from __future__ import annotations

import html
import re
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
    parts: list[str] = []
    parts.append(
        f'<div style="background:{BG};padding:14px 12px;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\','
        f'\'Helvetica Neue\',Helvetica,Arial,sans-serif;color:{NAVY};'
        f'line-height:1.6;font-size:15px;">'
    )
    parts.append(_theme_header(analysis, ref))
    parts.append(_theme_overview(analysis))
    if analysis.all_profiles:
        parts.append(_theme_factor_card(analysis))
    parts.append(_theme_ai_card(analysis))
    parts.append(_theme_supervision_card(analysis))
    parts.append(_theme_provenance_card(analysis))
    parts.append(_theme_disclaimer_card(analysis))
    parts.append("</div>")
    return "".join(parts)


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
                f'<div style="background:#dde5f0;border-radius:3px;height:6px;margin-top:3px;">'
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
            f'<span style="display:inline-block;background:#dde5f0;'
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
        f'<span style="display:inline-block;background:#dde5f0;color:{color};'
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
        f'<div style="background:#f3e9e9;border:1px solid #e0c9c9;'
        f'border-radius:8px;padding:10px 12px;font-size:11px;color:#7a4a45;">'
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
                f'<div style="font-size:14px;font-weight:700;color:{ACCENT};'
                f'margin:10px 0 4px;">【{title}】</div>'
            )
            if rest:
                out.append(f"<div>{rest}</div>")
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
