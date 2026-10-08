"""推送 HTML 渲染 —— DOS 复古 CRT 监视器终端页。

排版取向：整页仿 2000 年代 CRT 显示器里的 DOS 文本窗口 —— 纯黑屏底、磷光绿正文、
等宽字体、反白选中的强调条、标题栏、扫描线与荧光晕；页面**不固定尺寸**，宽度跟随
设备、高度由内容自然撑开（不再裁成 300×400 之类的墨水屏卡片）。

微信内置浏览器会剥掉 <style> 标签，所有样式必须写成内联 style；因此扫描线/晕光只用
可以内联的 background-image 渐变与 text-shadow 实现，动画、脚本、flex/grid 一律不用，
布局仅依赖 div/table 与基础盒模型。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from .models import ANALYSIS_BRIEF, Item, SourceResult, TimeQuality
from .timeutil import humanize, stamp

# --- DOS / CRT 监视器配色 --------------------------------------------------
# 黑屏 + 磷光绿正文，直角边框、反白选中条，一律不用圆角与彩色底。
# 涨跌与风险沿用 DOS 文本模式的 CGA 亮色：涨=亮红、跌=亮青、警示=琥珀，
# 在 CRT 上三色拉开得最开，缩到小屏也不会糊成一团。
BG = "#000000"           # 页面最外层：关机时的那块黑
SCREEN = "#070b08"       # 屏幕玻璃：通电后的黑，略微泛绿
CARD_BG = "#0b120c"      # 每扇终端窗口的面板
TITLEBAR_BG = "#0f2a17"  # 窗口标题栏
TITLEBAR_TEXT = "#7dffa8"
ROW_BG_A = "#111a12"     # 相邻两条底色：亮一档
ROW_BG_B = "#080d09"     # 相邻两条底色：暗一档
INK = "#c8f0c8"          # 正文：偏白的磷光绿，长文不刺眼
INK_BRIGHT = "#ffffff"   # 标题：文本模式亮白
INK_DIM = "#63a86f"      # 次要信息：暗一档
INK_FAINT = "#3f7a4c"    # 提示与注脚
WHITE = INK_BRIGHT       # 亮白（与 INK_BRIGHT 同一档，历史命名保留）
BORDER = "#1f7a3c"       # 磷光绿描边
BORDER_SOFT = "#123f22"
ACCENT = "#3dff82"       # 主强调色：终端绿
ACCENT_TEXT = "#03130a"  # 反白选中条上的字（DOS 选区就是反色）
ACCENT_BG = "#3dff82"
ACCENT_WASH = "#12291a"  # 弱底纹
SURFACE_ALT = "#0d1810"
CODE_BG = "#05100a"
CODE_TEXT = "#8fffb4"
HEADLINE_BG = "#3dff82"  # 一句人话：整条反白，屏幕上最亮的一行
HEADLINE_TEXT = "#03130a"
QUOTE_BG = "#0a140d"
WARN_BG = "#1b1306"      # 风险提示：琥珀色告警屏
WARN_BORDER = "#ffb000"
WARN_TEXT = "#ffd479"
UP = "#ff5f5f"           # 涨 / 偏多 / 高风险
DOWN = "#5fd7ff"         # 跌 / 偏空 / 低风险
AMBER = "#ffb000"        # 琥珀 CRT：标签、待核对、降级
CYAN = "#5fd7ff"         # 链接（与「跌」同用 CGA 亮青）

#: 等宽字体栈 —— DOS 味道的骨架。西文走 Courier New / Lucida Console，
#: 中文在 Windows 上回落到宋体（点阵感），iOS/Android 回落到各自等宽体。
MONO = "'Courier New',Courier,'Lucida Console','NSimSun','SimSun',monospace"

#: 扫描线：CRT 逐行扫描留下的横纹。只能写成内联 background-image，
#: 叠在面板底色之上、文字之下；内核不支持渐变时退化成纯黑屏，不影响读。
SCANLINE = (
    "background-image:repeating-linear-gradient(180deg,"
    "rgba(0,0,0,.45) 0px,rgba(0,0,0,.45) 1px,"
    "rgba(0,0,0,0) 1px,rgba(0,0,0,0) 3px);"
)

#: 荧光晕：磷光体被电子束打中后向四周洇开的那圈光。text-shadow 可继承，
#: 挂在最外层一次即可。
GLOW = "text-shadow:0 0 3px rgba(61,255,130,.35);"

#: 页面不固定尺寸：宽度 100% 跟随设备，高度由内容撑开，只留一点内边距。
#: inset 阴影是 CRT 的四角暗角（屏幕中心亮、边上糊下去），外圈那层是整机在发亮。
SHELL = (
    f'width:100%;box-sizing:border-box;margin:0;padding:9px 8px 12px;'
    f'background:{BG};{SCANLINE}font-family:{MONO};color:{INK};'
    f'font-size:14px;line-height:1.75;letter-spacing:.2px;{GLOW}'
    f'box-shadow:inset 0 0 70px rgba(0,0,0,.85);'
    f'word-break:break-word;overflow-wrap:anywhere;text-align:left;'
)

#: 一扇终端窗口的屏面：扫描线、直角、内层压暗 + 外圈荧光晕。
#: 底色与描边颜色由 `_window` 的调用方给（风险提示那扇窗是琥珀边）。
PANEL = (
    f'{SCANLINE}border-radius:0;'
    f'box-shadow:inset 0 0 26px rgba(0,0,0,.75),0 0 10px rgba(61,255,130,.13);'
)

#: 光标块：DOS 屏幕右下角那枚实心方块，用反色空格画出来（没有动画也能看见）。
CURSOR = f'<span style="background:{INK};color:{INK};font-size:13px;">&nbsp;█</span>'

#: 全部推送统一使用的固定标题：微信通知栏横幅只显示这一行，
#: 定时抓取、手动分析、合并研报、主题因子分析四种推送共用同一口径。
PUSH_TITLE = "章鱼 AI · 全景分析（实时事件因子）"

MANUAL_TITLE = "章鱼 AI 全景分析"
MANUAL_SUBTITLE = "全网 AI 调研境内境外数据，由多个大模型混合部署。"
MANUAL_FOOTER_AUTHOR = "作者：章鱼 ai      仅供参考，分析研究"
MANUAL_FOOTER_NOTE = (
    "全网境内外为你寻找蛛丝马迹-提供全景视野分析，由多模型协同推理决策，"
    "底层所使用的大语言模型（LLM）多模式背后结合使用了多种不同的先进模型，"
    "包括但不限于 Claude、ChatGPT、Gemini、Grok、Qwen 以及 Kimi。"
    "根据不同的资产管理任务需求，更好地发挥各个模型的优势来提供数据支持！[加油]"
)

#: 时间可信度角标：准确=终端绿、推算=琥珀、当日=亮青，都是 CGA 面板上的原色。
TIME_BADGE = {
    TimeQuality.EXACT: ("准确", ACCENT),
    TimeQuality.DERIVED: ("推算", AMBER),
    TimeQuality.DATE: ("当日", CYAN),
}

# 每张顶层卡片之间插入一个不可见标记。PushPlus 正文过长时，通知层只在
# 这些边界分页，绝不再从一段 HTML 的中间硬截断（硬截断会让微信正文样式错乱）。
HTML_BLOCK_SEPARATOR = "<!--octopus:block-->"


# ---------------------------------------------------------------------------
# DOS 终端外壳：窗口标题栏、标签、命令行、ASCII 进度条
# 所有构件一律自适应宽度（width:100%），不预设屏幕尺寸，长内容往下撑。
# ---------------------------------------------------------------------------


def _titlebar(caption: str, hint: str = "") -> str:
    """窗口标题栏：左边程序名，右边状态文字与 Win9x 那三枚按钮。

    两端对齐用两列 table —— 微信端 float 不稳，flex 更不稳。
    """
    buttons = "".join(
        f'<span style="display:inline-block;background:{TITLEBAR_TEXT};color:{SCREEN};'
        f'padding:0 5px;margin-left:3px;font-size:10px;font-weight:700;'
        f'line-height:14px;">{ch}</span>'
        for ch in ("_", "□", "×")
    )
    right = (
        f'<span style="color:{INK_DIM};font-size:10px;letter-spacing:.6px;'
        f'white-space:nowrap;margin-right:4px;">{hint}</span>'
        if hint
        else ""
    )
    return (
        f'<table style="width:100%;border-collapse:collapse;background:{TITLEBAR_BG};'
        f'border-bottom:1px solid {BORDER};"><tr>'
        f'<td style="padding:3px 7px;font-size:11px;font-weight:700;letter-spacing:.8px;'
        f'color:{TITLEBAR_TEXT};white-space:nowrap;overflow:hidden;">{caption}</td>'
        f'<td style="padding:3px 7px;text-align:right;white-space:nowrap;">{right}{buttons}</td>'
        f"</tr></table>"
    )


def _window(
    body: str,
    *,
    caption: str = "",
    hint: str = "",
    pad: str = "10px 11px",
    background: str = CARD_BG,
    border: str = BORDER,
    margin: str = "0 0 11px",
) -> str:
    """一扇自适应宽度的 DOS 终端窗口：标题栏 + 内容区，整块跟着正文变长。"""
    bar = _titlebar(caption, hint) if caption else ""
    inner = f'<div style="padding:{pad};">{body}</div>' if body else ""
    return (
        f'<div style="box-sizing:border-box;width:100%;margin:{margin};'
        f'background:{background};{PANEL}border:1px solid {border};'
        f'overflow:hidden;">{bar}{inner}</div>'
    )


def _chip(
    text: str,
    *,
    bg: str = ACCENT_BG,
    fg: str = ACCENT_TEXT,
    size: int = 11,
    margin: str = "0 4px 0 0",
) -> str:
    """反白小标签：DOS 里选中文本就是反色，比彩色底色更醒目。"""
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'padding:0 5px;margin:{margin};font-size:{size}px;font-weight:700;'
        f'letter-spacing:.4px;vertical-align:1px;">{text}</span>'
    )


def _outline(
    text: str,
    *,
    color: str = AMBER,
    size: int = 11,
    margin: str = "0 4px 0 0",
) -> str:
    """描边小标签：只有一圈线，用于次要状态（推算、单一来源、待核对）。"""
    return (
        f'<span style="display:inline-block;border:1px solid {color};color:{color};'
        f'padding:0 4px;margin:{margin};font-size:{size}px;letter-spacing:.4px;'
        f'vertical-align:1px;">{text}</span>'
    )


def _prompt(cmd: str, note: str = "", *, path: str = "C:\\OCTOPUS&gt;") -> str:
    """把这一轮动作写成一条 DOS 命令行：``C:\\OCTOPUS> SCAN /W=180``。"""
    tail = f' <span style="color:{INK_DIM};">{note}</span>' if note else ""
    return (
        f'<span style="color:{INK_FAINT};">{path}</span> '
        f'<span style="color:{ACCENT};font-weight:700;">{cmd}</span>{tail}'
    )


def _ascii_bar(score: float | None, *, width: int = 20, color: str = ACCENT) -> str:
    """``██████░░░░░░░░`` —— DOS 进度条只有实心块与空心块，没有渐变。"""
    filled = 0 if score is None else max(0, min(width, round(score / 100 * width)))
    return (
        f'<span style="color:{color};">{"█" * filled}</span>'
        f'<span style="color:{BORDER_SOFT};">{"░" * (width - filled)}</span>'
    )


def _status(text: str, *, color: str = ACCENT) -> str:
    """``[OK]`` 这类方括号状态位，终端里判断成败就看它。"""
    return (
        f'<span style="color:{INK_FAINT};">[</span>'
        f'<span style="color:{color};font-weight:700;">{text}</span>'
        f'<span style="color:{INK_FAINT};">]</span>'
    )


def _document(cards: list[str]) -> str:
    """把各内容块输出成一整屏滚动的 DOS 终端页，保留安全分页边界。

    页面**不固定尺寸**：宽度 100% 跟随设备（手机、平板、桌面网页版都撑满），
    高度由正文自然决定。每张顶层卡片是一扇独立的终端窗口，窗口之间插入
    分页标记，供推送层在超长正文里安全截断。
    """
    # 只包一层 div：推送层的截断逻辑靠「首个 > 到末个 </div>」找回外壳，
    # 多套一层会在截断后留下没闭合的标签（微信收到就是排版错乱）。
    return f'<div style="{SHELL}">{HTML_BLOCK_SEPARATOR.join(cards)}</div>'


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
        # 一条情报一页，避免把同一来源的多条新闻挤进一张超长卡片。
        for _result, items in groups:
            cards.extend(_row(item, ref, index=i) for i, item in enumerate(items))

    cards.append(_footer(ref, failures, degraded, window_minutes))
    return _document(cards)


# ---------------------------------------------------------------------------
# 头部 / 单条情报 / 页脚 —— 每一条情报都是一扇独立的终端窗口
# ---------------------------------------------------------------------------


def _header(total: int, window_minutes: int, ref: datetime) -> str:
    window_text = _window_text(window_minutes)
    stats = (
        f'<div style="margin-top:8px;border-top:1px solid {BORDER_SOFT};padding-top:7px;'
        f'font-size:12px;color:{INK_DIM};">'
        f'<span style="color:{INK_FAINT};">扫描时间</span> {stamp(ref)}（北京时间）'
        f'<span style="color:{INK_FAINT};"> · 时间窗口</span> {window_text}</div>'
        f'<div style="margin-top:5px;font-size:13px;color:{INK};">'
        f'<span style="color:{INK_FAINT};">本轮新增</span> '
        f'<span style="background:{ACCENT_BG};color:{ACCENT_TEXT};'
        f'padding:1px 9px;font-size:17px;font-weight:700;">{total}</span> 条'
        f'<span style="color:{INK_FAINT};font-size:11px;"> · 全部条目已校验发布时间</span>'
        f"</div>"
    )
    body = (
        f'<div style="font-size:20px;font-weight:700;color:{WHITE};letter-spacing:1px;">'
        f'<span style="color:{ACCENT};">█</span> 章鱼 AI · 个股雷达</div>'
        f'<div style="margin-top:7px;font-size:12px;">{_prompt("RADAR.EXE /SCAN /MODE=EVENT")}'
        f'&nbsp;{_status("ONLINE")}</div>'
        f"{stats}"
    )
    return _window(body, caption="RADAR.EXE", hint="PHOSPHOR")


def _section(result: SourceResult, items: list[Item], ref: datetime) -> str:
    """一扇窗口装下同一来源的全部条目，但不渲染任何同级来源标题。

    「东方财富 · 12 条」这类来源标题整级隐藏：正文直接从条目标题开始，
    十个源的条目按抓取顺序一块接一块排下来，靠交替底色区分条目、靠窗口区分源。
    降级说明不在这里挂带来源名的注脚了——页脚 `_footer` 已经统一交代
    （「降级说明：东方财富…」），正文再写一遍只是重复。
    """
    rows = "".join(_row(item, ref, index=i) for i, item in enumerate(items))
    return _window(rows, caption="SIGNAL.LOG")


def _row_bg(index: int) -> str:
    """相邻两条内容的底色：奇数条亮一档，偶数条暗一档，前后两条一眼分得开。"""
    return ROW_BG_A if index % 2 == 0 else ROW_BG_B


def _row(item: Item, ref: datetime, *, index: int = 0) -> str:
    """一条情报 = 一扇自适应宽度的终端窗口。

    ``index`` 是本条在所属源里的序号，用来取交替底色：屏幕上相邻两行一深一浅，
    不靠细虚线也分得开。窗口不设固定宽高，正文多长就撑多高。
    """
    title = html.escape(item.title)
    if item.url:
        title_html = (
            f'<a href="{html.escape(item.url, quote=True)}" '
            f'style="color:{WHITE};text-decoration:none;font-weight:700;'
            f'border-bottom:1px dotted {BORDER};">{title}</a>'
        )
    else:
        title_html = f'<span style="color:{WHITE};font-weight:700;">{title}</span>'

    when = humanize(item.published_at, ref=ref) if item.published_at else "—"
    exact = f"{item.published_at:%m-%d %H:%M}" if item.published_at else ""
    badge_text, badge_color = TIME_BADGE.get(item.time_quality, ("", INK_DIM))

    meta = (
        _chip(when)
        + f'<span style="color:{INK_DIM};font-size:12px;">{exact}</span>'
    )
    if badge_text and item.time_quality is not TimeQuality.EXACT:
        meta += _outline(badge_text, color=badge_color, margin="0 0 0 5px")

    tags_html = ""
    if item.tags:
        chips = "".join(
            _chip(html.escape(str(tag)), bg=ACCENT_WASH, fg=AMBER) for tag in item.tags[:3]
        )
        tags_html = f'<div style="margin-top:5px;">{chips}</div>'

    quote_html = _quote_line(item)
    related_html = _related_block(item)
    ai_html = _ai_block(item)

    summary_html = ""
    analysis_text = (item.ai_analysis or "").strip()
    summary_text = (item.summary or "").strip()
    if summary_text and summary_text not in analysis_text:
        summary_html = (
            f'<div style="font-size:13px;color:{INK};margin-top:5px;line-height:1.7;">'
            f"{html.escape(summary_text)}</div>"
        )

    inner = (
        f'<div style="font-size:15px;font-weight:600;line-height:1.6;">'
        f'<span style="color:{ACCENT};">&gt;</span> {title_html}</div>'
        f"{quote_html}{summary_html}{related_html}"
        f'<div style="font-size:12px;margin-top:5px;">{meta}</div>'
        # AI 那一块（一句人话 + 分析）合并后排在每条新闻最后
        f"{tags_html}{ai_html}"
    )
    panel = (
        f'<div style="background:{_row_bg(index)};border:1px solid {BORDER_SOFT};'
        f'padding:8px 9px;">{inner}</div>'
    )
    return _window(panel, caption="SIGNAL.LOG", hint=f"LINE {index + 1:03d}")


def _headline_line(text: str, label: str = "") -> str:
    """一句人话：整条反白（DOS 的选中态），在合并块里当引子，一眼就能扫到。

    ``label`` 为空时不挂小标签（整块只剩这一句时，块级标签已经写过同样的话）。
    """
    chip = (
        f'<span style="display:inline-block;background:{HEADLINE_TEXT};color:{HEADLINE_BG};'
        f'padding:0 5px;margin-right:7px;font-size:11px;font-weight:700;'
        f'vertical-align:1px;">{label}</span>'
        if label
        else ""
    )
    return (
        f'<div style="background:{HEADLINE_BG};padding:7px 9px;margin-bottom:6px;'
        f'border:1px solid {HEADLINE_BG};box-shadow:0 0 12px rgba(61,255,130,.25);">'
        f"{chip}"
        f'<span style="font-size:14px;font-weight:700;color:{HEADLINE_TEXT};'
        f'line-height:1.6;">{html.escape(text)}</span></div>'
    )


def _quote_line(item: Item) -> str:
    """现价行：没有对应标的或行情缺失时整行省略，绝不写假数字。"""
    if item.last_price is None or item.last_price <= 0:
        return ""
    chg = item.price_change
    if chg is None:
        color = INK
        chg_html = ""
    elif chg > 0:
        color = UP
        chg_html = f'<span style="color:{color};margin-left:4px;">▲ +{chg:.2f}%</span>'
    elif chg < 0:
        color = DOWN
        chg_html = f'<span style="color:{color};margin-left:4px;">▼ {chg:.2f}%</span>'
    else:
        color = INK_DIM
        chg_html = f'<span style="color:{color};margin-left:4px;">-- 0.00%</span>'

    name = html.escape((item.price_name or "").strip())
    code = html.escape((item.price_code or "").strip())
    if name and code:
        label = f"{name} {code}"
    else:
        label = name or code
    label_html = (
        f'<span style="color:{INK_DIM};margin-right:6px;">{label}</span>' if label else ""
    )
    return (
        f'<div style="font-size:12px;margin-top:5px;color:{INK};">'
        f'{_chip("现价", margin="0 6px 0 0")}'
        f"{label_html}"
        f'<span style="color:{color};font-weight:700;">{item.last_price:.2f}</span>'
        f"{chg_html}</div>"
    )


def _related_block(item: Item) -> str:
    """列出同题报道与网上相似观点；没有就如实标明证据不足。"""
    from .crossref import describe_related

    related = list(item.related or [])
    if not related:
        if not item.related_searched:
            return ""
        return (
            f'<div style="font-size:12px;color:{INK_DIM};margin-top:5px;">'
            f'{_outline("单一来源", color=INK_DIM, margin="0 6px 0 0")}'
            f"其它源与外部检索暂未见同题报道或相似观点</div>"
        )

    same = sum(1 for r in related if r.relation == "same_event")
    views = sum(1 for r in related if r.relation == "similar_viewpoint")
    labels = ([f"多源 {same}"] if same else []) + ([f"观点 {views}"] if views else [])
    head_label = " · ".join(labels) or "同标的"
    rows: list[str] = []
    for row_index, rel in enumerate(related):
        label = html.escape(describe_related(rel))
        title = html.escape(rel.title)
        parsed_url = urlsplit(rel.url) if rel.url else None
        safe_url = rel.url if parsed_url and parsed_url.scheme.lower() in ("http", "https") else ""
        if safe_url:
            title = (
                f'<a href="{html.escape(safe_url, quote=True)}" '
                f'style="color:{CYAN};text-decoration:none;">{title}</a>'
            )
        when = f"{rel.published_at:%m-%d %H:%M}" if rel.published_at else ""
        if rel.relation == "similar_viewpoint":
            score = f" · 相关度 {rel.similarity:.0%}" if rel.similarity is not None else ""
            note = f"（相似观点{score}）"
        elif rel.relation == "same_event":
            note = ""
        else:
            note = "（同标的，待核对）"
        summary = ""
        if rel.summary:
            snippet = rel.summary[:120].rstrip("，,；;、 ") + ("…" if len(rel.summary) > 120 else "")
            summary = (
                f'<div style="color:{INK_DIM};font-size:11px;margin-left:12px;">'
                f"公开摘要：{html.escape(snippet)}</div>"
            )
        branch = "└─" if row_index == len(related) - 1 else "├─"
        rows.append(
            f'<div style="margin-top:2px;">'
            f'<span style="color:{BORDER};">{branch}</span>'
            f'<span style="color:{INK_DIM};"> {label}</span> {title}'
            f'<span style="color:{INK_FAINT};font-size:11px;"> {when}{note}</span>'
            f"{summary}</div>"
        )
    return (
        f'<div style="font-size:12px;color:{INK};margin-top:5px;line-height:1.7;">'
        f'{_chip(head_label, bg=ACCENT_WASH, fg=ACCENT, margin="0 5px 0 0")}'
        f'<span style="color:{INK_DIM};font-size:11px;">同题报道与网上相似观点</span>'
        f"{''.join(rows)}</div>"
    )


#: 分析块里会分行排版的模块标签：证券五模块、总编简报三模块，
#: 括号里最后是升级前的六字段，保证历史输出仍能被分行排版。
_SECURITY_ANALYSIS_MARK = re.compile(
    r"【(事件重塑|利弊挖掘|深度溯源|多维推演|事实核查"
    r"|核心快讯|关键要素|发展脉络"
    r"|板块|概念|相似观点|看多|看空|逻辑)】"
)
#: 多维推演里的概率按 CGA 亮色区分：偏多亮红、偏空亮青，与现价涨跌同一套颜色。
_PROB_UP = re.compile(r"(偏多\s*)(\d{1,3}\s*[%％])")
_PROB_DOWN = re.compile(r"(偏空\s*)(\d{1,3}\s*[%％])")


def _security_analysis_html(value: str) -> str:
    """把证券分析五模块排成终端逐行输出的样子；旧自由文本仍按原样安全转义。"""
    matches = list(_SECURITY_ANALYSIS_MARK.finditer(value or ""))
    if not matches:
        return html.escape(value).replace("\n", "<br>")

    colors = {"看多": UP, "看空": DOWN}
    rows: list[str] = []
    prefix = value[: matches[0].start()].strip(" \n；;")
    if prefix:
        rows.append(f'<div style="margin-bottom:3px;color:{INK_DIM};">{html.escape(prefix)}</div>')
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        name = match.group(1)
        body = value[match.end() : end].strip(" \n；;")
        if not body:
            continue  # 模块标签后面没内容就不显示这一行，不留空标签
        color = colors.get(name, ACCENT)
        body_html = html.escape(body).replace(chr(10), "<br>")
        body_html = _PROB_UP.sub(
            rf'\1<span style="color:{UP};font-weight:700;">\2</span>', body_html
        )
        body_html = _PROB_DOWN.sub(
            rf'\1<span style="color:{DOWN};font-weight:700;">\2</span>', body_html
        )
        rows.append(
            f'<div style="margin-top:{"2" if len(rows) == 0 else "5"}px;">'
            f'<span style="display:inline-block;min-width:64px;color:{color};font-weight:700;'
            f'vertical-align:top;">【{name}】</span>'
            f'<span style="color:{INK};">{body_html}</span>'
            f"</div>"
        )
    return "".join(rows)


def _ai_block(item: Item) -> str:
    """AI 那一块：一句人话 + 分析合并成一个块，排在每条新闻最后。

    块级标签如实交代体裁与来源：模型五模块标「AI 分析」、模型总编简报标「AI 简报」、
    规则化降级标「分析」，都不假装；块里那句人话另挂「AI 一句话」/「一句话」小标签。
    两段都没内容时整块不显示 —— 兜底不凑字数。
    """
    headline = (item.ai_headline or "").strip()
    analysis = (item.ai_analysis or "").strip()
    body_html = _security_analysis_html(analysis).strip() if analysis else ""
    if not headline and not body_html:
        return ""

    headline_label = "AI 一句话" if item.ai_headline_from_model else "一句话"
    if body_html:
        if not item.ai_analysis_from_model:
            label = "分析"  # 规则化兜底，不假装用了大模型
        elif item.ai_analysis_kind == ANALYSIS_BRIEF:
            label = "AI 简报"
        else:
            label = "AI 分析"
    else:
        label = headline_label  # 整块只剩一句人话，不必再挂一枚同样的小标签

    rows = ""
    if headline:
        rows += _headline_line(headline, "" if not body_html else headline_label)
    rows += body_html
    return (
        f'<div style="font-size:13px;color:{INK};margin-top:7px;line-height:1.75;'
        f'background:{SURFACE_ALT};border:1px dashed {BORDER};padding:6px 8px;">'
        f'<div style="margin-bottom:5px;">{_chip(label, margin="0")}</div>'
        f"{rows}</div>"
    )


def _empty_card(window_minutes: int) -> str:
    body = (
        f'<div style="text-align:center;font-size:17px;font-weight:700;color:{WHITE};'
        f'letter-spacing:1.5px;">[ NO NEW SIGNAL ]</div>'
        f'<div style="text-align:center;font-size:13px;color:{INK};margin-top:7px;">'
        f"本轮无新增内容</div>"
        f'<div style="text-align:center;font-size:12px;color:{INK_DIM};margin-top:6px;'
        f'line-height:1.75;">十个源均已扫描，{_window_text(window_minutes)}内没有未推送过的新条目。'
        f"抓取程序运行正常。</div>"
        f'<div style="text-align:center;font-size:12px;margin-top:10px;color:{INK_FAINT};">'
        f"C:\\OCTOPUS&gt; pause {CURSOR}</div>"
    )
    return _window(body, caption="RADAR.EXE", hint="IDLE")


def _footer(
    ref: datetime,
    failures: list[SourceResult],
    degraded: list[SourceResult],
    window_minutes: int,
) -> str:
    lines: list[str] = []
    if degraded:
        notes = "；".join(f"{r.source_label}{r.degraded}" for r in degraded if r.degraded)
        if notes:
            lines.append(f"降级说明：{notes}")
    if failures:
        names = "、".join(r.source_label for r in failures)
        lines.append(f"本轮未取到数据：{names}（已自动重试，下轮继续）")

    body = "".join(
        f'<div style="margin-top:3px;font-size:12px;color:{AMBER};">'
        f'<span style="color:{INK_FAINT};">&gt;</span> {html.escape(line)}</div>'
        for line in lines
    )
    research_note = (
        "偏多/偏空情景概率是基于当前公开材料的事件情景权重，不是统计预测或收益承诺；"
        "仅供研究参考，不构成投资建议。"
    )
    tail = (
        f'<div style="margin-top:7px;padding-top:6px;border-top:1px solid {BORDER_SOFT};'
        f'font-size:11px;color:{INK_DIM};line-height:1.7;">{research_note}</div>'
        f'<div style="margin-top:7px;font-size:12px;color:{INK_FAINT};">'
        f"C:\\OCTOPUS&gt; {CURSOR}</div>"
    )
    return _window(
        body + tail,
        caption="README.TXT",
        hint=stamp(ref)[:16],
    )


def _window_text(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def render_title(total: int, ref: datetime, top: Item | None) -> str:
    """定时抓取推送的标题。

    全场景统一固定标题：微信通知栏只显示这一行，条数、时间、头条等
    参数仅保留在签名中（调用方与历史数据兼容），不再参与拼接。
    """
    return PUSH_TITLE


# ---------------------------------------------------------------------------
# 手动主题分析推送：人工录入 AI 分析内容，直接渲染成一条独立推送。
# 与抓取推送共用同一套 DOS 终端外壳（黑屏磷光绿、宽度自适应、高度随正文），
# 但不经过时间校验与去重。
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
        chunks = _split_card_text(content)
        cards.extend(
            _manual_card(body_title if index == 0 else f"{body_title}（续 {index + 1}）", chunk)
            for index, chunk in enumerate(chunks)
        )
    cards.append(_manual_footer())
    return _document(cards)


def _split_card_text(text: str, max_chars: int = 900) -> list[str]:
    """把长正文切成一屏读得完的若干页，优先在段落/句子边界换页。

    页面本身不固定尺寸，切页只是为了让推送层有稳定的分页边界
    （PushPlus 超长时只在块边界截断）与阅读节奏，不裁内容。
    """
    remaining = (text or "").strip()
    if not remaining:
        return [""]
    chunks: list[str] = []
    boundaries = "\n。！？；.!?;，, "
    while len(remaining) > max_chars:
        cut = max_chars
        lower_bound = int(max_chars * 0.65)
        candidates = [remaining.rfind(mark, lower_bound, max_chars + 1) for mark in boundaries]
        best = max(candidates, default=-1)
        if best >= lower_bound:
            cut = best + 1
        chunk = remaining[:cut].strip()
        if not chunk:
            chunk = remaining[:max_chars].strip()
            cut = max_chars
        chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _manual_header(topic: str, ai_model: str = "") -> str:
    topic_html = ""
    if topic:
        topic_html = (
            f'<div style="margin-top:10px;background:{SURFACE_ALT};'
            f'border:1px solid {BORDER_SOFT};padding:8px 9px;">'
            f'<div style="font-size:10px;letter-spacing:1.2px;color:{INK_FAINT};">本期主题</div>'
            f'<div style="font-size:14px;font-weight:700;color:{WHITE};margin-top:3px;'
            f'line-height:1.6;">{html.escape(topic)}</div></div>'
        )
    model_html = (
        f'<div style="font-size:11px;color:{INK_DIM};margin-top:8px;">'
        f'<span style="color:{INK_FAINT};">模型协同摘要：</span>{html.escape(ai_model)}</div>'
        if ai_model
        else ""
    )
    body = (
        f'<div style="display:inline-block;background:{ACCENT_BG};color:{ACCENT_TEXT};'
        f'padding:4px 12px;font-size:20px;font-weight:700;letter-spacing:1px;">'
        f"{MANUAL_TITLE}</div>"
        f'<div style="margin-top:8px;font-size:12px;color:{INK};line-height:1.75;">'
        f"{MANUAL_SUBTITLE}</div>"
        f"{model_html}{topic_html}"
    )
    head = (
        f'<div style="margin-top:9px;font-size:11px;">{_prompt("MANUAL.EXE /TYPE /MODEL=MIX")}'
        f'&nbsp;{_status("READY")}</div>'
    )
    return _window(body + head, caption="MANUAL.EXE", hint="USER INPUT")


def _manual_ai_card(ai_summary: str, ai_model: str) -> str:
    title = f"DeepSeek AI 智能提炼 · 模型协同摘要 [{html.escape(ai_model)}]"
    body = _rich_text(ai_summary)
    inner = (
        f'<div style="font-size:14px;font-weight:700;color:{WHITE};letter-spacing:.5px;'
        f'padding-bottom:7px;margin-bottom:8px;border-bottom:1px solid {BORDER};">'
        f"▍{title}</div>"
        f'<div style="font-size:14px;color:{INK};line-height:1.8;">{body}</div>'
    )
    return _window(inner, caption="AI_SUMMARY.LOG", hint="DEEPSEEK")


def _manual_card(topic: str, content: str) -> str:
    title = html.escape(topic) if topic else "正文"
    body = html.escape(content).replace("\n", "<br>")
    inner = (
        f'<div style="font-size:14px;font-weight:700;color:{WHITE};letter-spacing:.5px;'
        f'padding-bottom:7px;margin-bottom:9px;border-bottom:1px solid {BORDER};">'
        f"▍{title}</div>"
        f'<div style="font-size:14px;color:{INK};line-height:1.85;">{body}</div>'
    )
    return _window(inner, caption="MANUAL.TXT", hint="PAGE")


def _manual_footer() -> str:
    inner = (
        f'<div style="font-weight:700;color:{WHITE};margin-bottom:6px;font-size:12px;">'
        f"{MANUAL_FOOTER_AUTHOR}</div>"
        f'<div style="font-size:11px;color:{INK_DIM};line-height:1.8;">{MANUAL_FOOTER_NOTE}</div>'
        f'<div style="margin-top:8px;font-size:11px;color:{INK_FAINT};">'
        f"C:\\OCTOPUS&gt; {CURSOR}</div>"
    )
    return _window(inner, caption="README.TXT", background=SURFACE_ALT)


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
            f'<code style="background:{CODE_BG};color:{CODE_TEXT};border:1px solid {BORDER_SOFT};'
            f'padding:0 4px;font-family:{MONO};font-size:12px;">'
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
            f'<a href="{url}" style="background:{ACCENT_WASH};color:{CYAN};'
            f'border:1px solid {BORDER};padding:1px 6px;text-decoration:none;'
            f'font-weight:700;">{label}</a>'
        )

    escaped = re.sub(r"\[([^\]]+)]\(([^)\s]+)(?:\s+[^)]*)?\)", link, escaped)
    escaped = re.sub(
        r"\*\*(.+?)\*\*|__(.+?)__",
        lambda m: (
            f'<strong style="color:{WHITE};background:{ACCENT_WASH};">'
            f"{m.group(1) or m.group(2)}</strong>"
        ),
        escaped,
    )
    escaped = re.sub(
        r"~~(.+?)~~",
        r'<span style="text-decoration:line-through;">\1</span>',
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
    min_width = min(520, max(200, width * 110))
    head = "".join(
        f'<th style="background:{ACCENT};color:{ACCENT_TEXT};font-weight:700;'
        f'padding:5px 6px;border:1px solid {BORDER};text-align:left;vertical-align:top;'
        f'white-space:nowrap;">{_inline_markdown(cell)}</th>'
        for cell in normalized[0]
    )
    body_rows: list[str] = []
    for row_index, row in enumerate(normalized[1:]):
        bg = ROW_BG_A if row_index % 2 == 0 else ROW_BG_B
        cells = "".join(
            f'<td style="background:{bg};color:{INK};padding:5px 6px;'
            f'border:1px solid {BORDER_SOFT};vertical-align:top;">'
            f"{_inline_markdown(cell)}</td>"
            for cell in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f'<div style="overflow-x:auto;margin:9px 0;">'
        f'<table style="width:100%;min-width:{min_width}px;border-collapse:collapse;'
        f'font-size:12px;line-height:1.55;"><thead><tr>{head}</tr></thead>'
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
                f'<div style="font-size:10px;color:{INK_FAINT};margin-bottom:5px;'
                f'letter-spacing:1px;">; {html.escape(language)}</div>'
                if language
                else ""
            )
            rendered = (
                f'<div style="margin:9px 0;background:{CODE_BG};{SCANLINE}'
                f'border:1px solid {BORDER_SOFT};padding:9px 10px;color:{CODE_TEXT};">'
                f"{label}"
                f'<pre style="margin:0;white-space:pre-wrap;word-break:break-word;'
                f'font:12px/1.6 {MONO};color:{CODE_TEXT};">'
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
                size = 16 if level == 3 else 15
                rendered = (
                    f'<div style="font-size:{size}px;font-weight:700;color:{WHITE};'
                    f'letter-spacing:.5px;margin:14px 0 6px;padding:0 0 4px 0;'
                    f'border-bottom:1px solid {BORDER};">'
                    f'<span style="color:{ACCENT};">&gt;</span> '
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
                f'<div style="background:{QUOTE_BG};border:1px solid {BORDER_SOFT};'
                f'border-left:4px solid {ACCENT};padding:7px 10px;margin:8px 0;'
                f'font-size:13px;color:{INK_DIM};line-height:1.75;">'
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
                    f'<div style="padding:3px 0 3px {left}px;color:{INK};">'
                    f'<span style="display:inline-block;width:26px;box-sizing:border-box;'
                    f'margin-left:-26px;background:{ACCENT_BG};color:{ACCENT_TEXT};'
                    f'padding:0 4px;text-align:center;font-weight:700;">'
                    f"{html.escape(symbol)}</span>"
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
        paragraph_text = "\n".join(paragraph)
        for paragraph_chunk in _split_card_text(paragraph_text, max_chars=600):
            value = "<br>".join(
                _inline_markdown(part) for part in paragraph_chunk.splitlines()
            )
            # 大模型常用【模块名】作行首标题，单独强调，避免所有文字挤成一团。
            value = re.sub(
                r"^【([^】]+)】\s*",
                rf'<strong style="background:{ACCENT_BG};color:{ACCENT_TEXT};'
                rf'padding:1px 6px;">【\1】</strong> ',
                value,
            )
            blocks.append(
                _MarkdownBlock(
                    "paragraph",
                    f'<div style="font-size:14px;color:{INK};margin:5px 0;line-height:1.8;">'
                    f"{value}</div>",
                )
            )
    return blocks


def _markdown_card(title: str, body: str, *, continued: bool = False) -> str:
    suffix = " · 续" if continued else ""
    inner = (
        f'<div style="font-size:15px;font-weight:700;color:{WHITE};letter-spacing:.5px;'
        f'padding-bottom:7px;margin-bottom:8px;border-bottom:1px solid {BORDER};">'
        f"▍{_inline_markdown(title)}{suffix}</div>{body}"
    )
    hint = "CONTINUED" if continued else "SECTION"
    return _window(inner, caption="REPORT.MD", hint=hint)


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
    # 卡片正文按实际可见字符数限量，而不是按含大量内联样式的 HTML 字符数计算。
    # 页面宽度自适应后不再按窄屏裁切，只保留一个「一屏读得完」的宽松上限。
    max_card_chars = 700

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
        visible_text = html.unescape(re.sub(r"<[^>]+>", "", block.rendered))
        block_size = len(visible_text)
        if current and current_size + block_size > max_card_chars:
            flush()
        current.append(block.rendered)
        current_size += block_size
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
    """把合并后的 Markdown 渲染成一屏滚动的 DOS 终端页（宽度自适应、高度随正文）。"""
    topic = (topic or "合并研报").strip()
    content = _push_report_markdown(content)
    ai_summary = (ai_summary or "").strip()
    merged = f" · 合并 {source_count} 份" if source_count else ""
    head = (
        f'<div style="font-size:20px;font-weight:700;color:{WHITE};letter-spacing:1px;">'
        f'<span style="color:{ACCENT};">█</span> 章鱼 AI · 合并研报</div>'
        f'<div style="margin-top:8px;display:inline-block;background:{ACCENT_BG};'
        f'color:{ACCENT_TEXT};padding:2px 10px;font-size:16px;font-weight:700;">'
        f"{html.escape(topic)}</div>"
        f'<div style="margin-top:8px;font-size:12px;color:{INK_DIM};">'
        f"{stamp(ref)}（北京时间）{merged}</div>"
        f'<div style="margin-top:6px;font-size:11px;">{_prompt("MERGE.EXE /MD /OUT=WECHAT")}</div>'
    )
    cards = [_window(head, caption="MERGE.EXE", hint="REPORT")]
    if ai_summary:
        cards.append(_manual_ai_card(ai_summary, ai_model))
    cards.extend(_markdown_cards(topic, content))
    cards.append(
        _window(
            f'<div style="font-size:12px;color:{INK_DIM};">仅供研究参考，不构成投资建议</div>'
            f'<div style="margin-top:6px;font-size:11px;color:{INK_FAINT};">'
            f"C:\\OCTOPUS&gt; {CURSOR}</div>",
            caption="MERGE.LOG",
        )
    )
    return _document(cards)


def render_merge_title(topic: str, ref: datetime, source_count: int = 0) -> str:
    """合并研报推送的标题：全场景统一固定标题。"""
    return PUSH_TITLE


def render_manual_title(topic: str, ref: datetime) -> str:
    """手动主题分析的推送标题：全场景统一固定标题。"""
    return PUSH_TITLE


# ---------------------------------------------------------------------------
# 主题因子分析推送：输入主题 -> 监管 + qlib 因子模型 -> AI 报告
# 沿用同一套 DOS 终端外壳（黑屏磷光绿、宽度自适应、高度随正文），结构上分为：
#   概览卡（主题/板块/数据日期）→ 因子雷达（维度评分表）→ AI 解读
#   → 监管视角 → 数据溯源与合规声明
# ---------------------------------------------------------------------------

#: 因子维度评分的配色档位：高分走亮磷光绿，中段转琥珀，低分到 CGA 红。
_SCORE_COLORS = (
    (70.0, "#3dff82"),   # 强
    (55.0, "#22cc66"),
    (45.0, "#ffb000"),   # 中性偏暗：琥珀
    (30.0, "#d98a1f"),
    (0.0, "#ff5f5f"),    # 弱
)

#: 监管风险等级同样用 CRT 三色：高=亮红、中=琥珀、偏低=亮青。
_RISK_COLORS = {"高": UP, "中": AMBER, "偏低": DOWN}


def _score_color(score: float | None) -> str:
    if score is None:
        return INK_DIM
    for threshold, color in _SCORE_COLORS:
        if score >= threshold:
            return color
    return INK_DIM


def render_theme(analysis, *, ref: datetime | None = None) -> str:
    """把一次主题因子分析渲染成推送正文。

    analysis 是 octopus.factor.pipeline.ThemeAnalysis；这里只读它的字段，
    不做任何计算 —— 渲染层保持哑管道，方便单测直接构造假对象。
    """
    ref = ref or analysis.ref
    cards: list[str] = [_theme_header(analysis, ref), _theme_overview(analysis)]
    if analysis.all_profiles:
        cards.append(_theme_factor_card(analysis))
    # 解读正文为空（大模型没返回、规则化也无话可说）时整卡不显示，不留空标题。
    if (analysis.ai_report or "").strip():
        cards.append(_theme_ai_card(analysis))
    cards.extend(
        [
            _theme_supervision_card(analysis),
            _theme_provenance_card(analysis),
            _theme_disclaimer_card(analysis),
        ]
    )
    return _document(cards)


def _theme_header(analysis, ref: datetime) -> str:
    topic = html.escape(analysis.topic or "未指定主题")
    engine = "DeepSeek 大模型解读" if analysis.used_ai else "内置规则化解读"
    body = (
        f'<div style="font-size:20px;font-weight:700;color:{WHITE};letter-spacing:1px;">'
        f'<span style="color:{ACCENT};">█</span> 章鱼 AI · 主题因子分析</div>'
        f'<div style="margin-top:8px;display:inline-block;background:{ACCENT_BG};'
        f'color:{ACCENT_TEXT};padding:2px 10px;font-size:16px;font-weight:700;">'
        f"{topic}</div>"
        f'<div style="margin-top:8px;font-size:12px;color:{INK_DIM};">'
        f"A股市场监督管理视角 · qlib Alpha158 因子模型 · {engine}</div>"
        f'<div style="font-size:12px;color:{INK_DIM};margin-top:3px;">'
        f"生成时间 {stamp(ref)}（北京时间）</div>"
        f'<div style="margin-top:7px;font-size:11px;">{_prompt("THEME.EXE /TOPIC /FACTOR=ALPHA158")}'
        f'&nbsp;{_status("RUN OK")}</div>'
    )
    return _window(body, caption="THEME.EXE", hint="PHOSPHOR")


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
    level_color = _RISK_COLORS.get(level, INK_DIM)
    sup = analysis.supervision
    rows.append(
        (
            "监管风险",
            f'<span style="color:{level_color};font-weight:700;">{level}</span>'
            f'<span style="color:{INK_DIM};"> · 标的相关 {len(sup.focus)} 条 / '
            f"全市场 {len(sup.events)} 条</span>",
        )
    )

    body = "".join(
        f'<div style="padding:4px 0;font-size:13px;border-bottom:1px solid {BORDER_SOFT};">'
        f'<span style="color:{INK_FAINT};display:inline-block;min-width:88px;">'
        f"{html.escape(label)}</span>"
        f'<span style="color:{INK};">{value}</span></div>'
        for label, value in rows
    )
    return _window(body, caption="OVERVIEW.CSV", hint="SUMMARY")


def _theme_factor_card(analysis) -> str:
    """因子评分卡：每个标的一张六维评分表，分数用 DOS 的 █░ 进度条画出来。"""
    blocks: list[str] = []
    for profile in analysis.all_profiles:
        if not profile.dimensions:
            blocks.append(
                f'<div style="padding:8px 0;border-bottom:1px solid {BORDER_SOFT};'
                f'font-size:13px;color:{INK_DIM};">'
                f'<span style="color:{AMBER};">!!</span> '
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
            f'<span style="font-size:15px;font-weight:700;color:{WHITE};">'
            f"{html.escape(profile.name)}</span>"
            f'<span style="font-size:11px;color:{INK_DIM};">'
            f"&nbsp;{html.escape(profile.code)}</span></td>"
            f'<td style="padding:0;text-align:right;vertical-align:bottom;'
            f'white-space:nowrap;">'
            f'<span style="background:{comp_color};color:{ACCENT_TEXT};'
            f'padding:1px 7px;font-size:16px;font-weight:700;">{comp_text}</span>'
            f'<span style="font-size:11px;color:{INK_DIM};">/100</span></td>'
            f"</tr></table>"
            f'<div style="font-size:12px;color:{INK_DIM};margin-bottom:6px;">'
            f"{html.escape(profile.stance)}</div>"
        )

        bars: list[str] = []
        for dim in profile.dimensions:
            score = dim.score
            color = _score_color(score)
            score_text = "—" if score is None else f"{score:.0f}"
            bars.append(
                f'<div style="margin:5px 0;padding:5px 7px;background:{ROW_BG_B};'
                f'border:1px solid {BORDER_SOFT};">'
                f'<div style="font-size:12px;color:{INK};">'
                f'<span style="display:inline-block;min-width:66px;color:{WHITE};'
                f'font-weight:700;">{html.escape(dim.label)}</span>'
                f'<span style="color:{color};font-weight:700;">{score_text}</span>'
                f'<span style="color:{INK_DIM};"> · {html.escape(dim.level)}</span></div>'
                f'<div style="font-size:12px;margin-top:2px;white-space:nowrap;'
                f'overflow:hidden;">{_ascii_bar(score, color=color)}</div>'
                f'<div style="font-size:11px;color:{INK_DIM};margin-top:3px;">'
                f"{html.escape(dim.detail)}</div>"
                f"</div>"
            )
        blocks.append(
            f'<div style="padding:6px 0;border-bottom:1px solid {BORDER_SOFT};">'
            f"{head}{''.join(bars)}</div>"
        )

    ranking = analysis.ranking()
    rank_html = ""
    if len(ranking) > 1:
        chips = "".join(
            f'<span style="display:inline-block;background:{ACCENT_WASH};'
            f'color:{_score_color(score)};border:1px solid {BORDER_SOFT};'
            f'padding:1px 6px;margin:2px 4px 2px 0;font-size:11px;font-weight:700;">'
            f"{html.escape(name)} {score:.0f}</span>"
            for name, score in ranking
        )
        rank_html = (
            f'<div style="margin-top:8px;font-size:11px;color:{INK_DIM};">'
            f"横截面因子分布（仅呈现分布，不构成推荐）</div>"
            f'<div style="margin-top:4px;">{chips}</div>'
        )

    inner = (
        f'<div style="font-size:15px;font-weight:700;color:{WHITE};letter-spacing:.5px;'
        f'padding-bottom:7px;margin-bottom:4px;border-bottom:1px solid {BORDER};">'
        f"▍量化因子评分"
        f'<span style="font-size:11px;color:{INK_DIM};font-weight:400;">'
        f" · qlib Alpha158</span></div>"
        f"{''.join(blocks)}{rank_html}"
    )
    return _window(inner, caption="FACTOR.LOG", hint="ALPHA158")


def _theme_ai_card(analysis) -> str:
    engine = (
        f"DeepSeek AI 解读 [{html.escape(analysis.ai_model)}]"
        if analysis.used_ai
        else "规则化因子解读 [未配置大模型 Key]"
    )
    body = _rich_text(analysis.ai_report)
    inner = (
        f'<div style="font-size:16px;font-weight:700;color:{WHITE};letter-spacing:.5px;'
        f'padding-bottom:7px;margin-bottom:10px;border-bottom:1px solid {BORDER};">'
        f"▍{engine}</div>"
        f'<div style="font-size:14px;color:{INK};line-height:1.8;">{body}</div>'
    )
    hint = "DEEPSEEK" if analysis.used_ai else "RULE-BASED"
    return _window(inner, caption="AI_REPORT.LOG", hint=hint)


def _theme_supervision_card(analysis) -> str:
    sup = analysis.supervision
    level = sup.risk_level
    level_color = _RISK_COLORS.get(level, INK_DIM)

    lines: list[str] = [
        f'<div style="font-size:13px;margin-bottom:6px;color:{INK};">'
        f'<span style="color:{INK_FAINT};">整体监管风险：</span>'
        f'<span style="background:{level_color};color:{ACCENT_TEXT};font-weight:700;'
        f'padding:0 6px;">{level}</span>'
        f'<span style="color:{INK_DIM};"> · {html.escape(sup.summary_line())}</span></div>'
    ]

    related = sup.focus
    if related:
        lines.append(
            f'<div style="font-size:12px;font-weight:700;color:{WHITE};'
            f'margin:8px 0 4px;letter-spacing:.5px;">&gt; 与分析标的直接相关</div>'
        )
        lines.extend(_supervision_row(e, index=i) for i, e in enumerate(related[:6]))
    focus_ids = {id(e) for e in related}
    others = [e for e in sup.events if id(e) not in focus_ids][:6]
    if others:
        lines.append(
            f'<div style="font-size:12px;font-weight:700;color:{WHITE};'
            f'margin:8px 0 4px;letter-spacing:.5px;">&gt; 同期市场监管动态</div>'
        )
        lines.extend(_supervision_row(e, index=i) for i, e in enumerate(others))
    if not sup.events:
        lines.append(
            f'<div style="font-size:12px;color:{INK_DIM};">'
            f'<span style="color:{INK_FAINT};">C:\\OCTOPUS&gt;</span> '
            f"近 {sup.window_days} 天未检出与本主题直接相关的监管事件"
            f"（已扫描 {sup.scanned} 条公告）</div>"
        )

    from .factor.supervision import policy_context

    policy = policy_context(analysis.topic)
    if policy:
        lines.append(
            f'<div style="font-size:12px;color:{AMBER};margin-top:8px;">'
            f"主题涉及政策敏感词：{html.escape('、'.join(policy))}，"
            f"请以监管部门正式发布口径为准</div>"
        )

    inner = (
        # padding-left 与 _supervision_row 的底色块内边距一致，标题与事件标题左侧对齐
        f'<div style="font-size:15px;font-weight:700;color:{WHITE};letter-spacing:.5px;'
        f'padding-left:8px;padding-bottom:7px;margin-bottom:8px;'
        f'border-bottom:1px solid {BORDER};">'
        f"▍A股市场监督管理</div>"
        f"{''.join(lines)}"
    )
    return _window(inner, caption="SUPERVISION.LOG", hint="CSRC WATCH")


def _supervision_row(event, *, index: int = 0) -> str:
    color = UP if event.severity >= 85 else (AMBER if event.severity >= 65 else INK_DIM)
    title = html.escape(event.title)
    if event.url:
        title = (
            f'<a href="{html.escape(event.url, quote=True)}" '
            f'style="color:{WHITE};text-decoration:none;">{title}</a>'
        )
    return (
        f'<div style="background:{_row_bg(index)};border:1px solid {BORDER_SOFT};'
        f'padding:6px 8px;margin-top:6px;">'
        f'{_chip(html.escape(event.category), bg=ACCENT_WASH, fg=color, size=11)}'
        f'<span style="font-size:13px;color:{INK};">{title}</span>'
        f'<div style="font-size:11px;color:{INK_FAINT};margin-top:2px;">'
        f'<span style="color:{INK_FAINT};">SEV</span> {event.severity:03d} · '
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
        f'<div style="margin-top:3px;">'
        f'<span style="color:{INK_FAINT};">&gt;</span> {html.escape(line)}</div>'
        for line in lines
        if (line or "").strip()  # 空行不显示，不留一个孤零零的「·」
    )
    inner = (
        f'<div style="font-weight:700;color:{WHITE};margin-bottom:5px;font-size:12px;'
        f'letter-spacing:.5px;">数据溯源与口径</div>{body}'
    )
    return _window(
        inner,
        caption="PROVENANCE.TXT",
        background=SURFACE_ALT,
        border=BORDER_SOFT,
        pad="9px 11px",
    )


def _theme_disclaimer_card(analysis) -> str:
    from .factor.compliance import disclaimer

    body = "".join(
        f'<div style="margin-top:4px;color:{WARN_TEXT};">{html.escape(line)}</div>'
        for line in disclaimer()
    )
    inner = (
        f'<div style="font-weight:700;color:{AMBER};margin-bottom:4px;font-size:12px;'
        f'letter-spacing:.5px;">⚠ 风险提示与免责声明</div>{body}'
    )
    return _window(
        inner,
        caption="WARNING.TXT",
        background=WARN_BG,
        border=WARN_BORDER,
        pad="9px 11px",
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
                f'<div style="font-size:14px;font-weight:700;color:{WHITE};'
                f'letter-spacing:.5px;margin:10px 0 4px;padding-bottom:3px;'
                f'border-bottom:1px solid {BORDER_SOFT};">'
                f'<span style="color:{ACCENT};">■</span>【{title}】{rest}</div>'
            )
            continue
        out.append(f'<div style="color:{INK};">{html.escape(stripped)}</div>')
    return "".join(out)


def render_theme_title(topic: str, analysis=None, ref: datetime | None = None) -> str:
    """主题因子分析推送的标题：全场景统一固定标题。

    标题用于微信通知栏展示，统一口径，避免主题、分数或风险等级变化导致
    通知标题不一致。
    """
    return PUSH_TITLE
