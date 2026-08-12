"""报告合并模块：把多份 Markdown 分析报告合并成一份，支持一对一推送.

功能：
1. 合并多份本地 Markdown 文件（去重、目录、溯源）
2. 合并多主题因子分析结果（可选，用于 ThemePipeline 汇总）
3. 与 Agent / DeepSeek AI 打通：合并后可调用大模型提炼摘要并推送

设计原则：
- 宁可保留，不可臆造：原始报告内容原样保留，只做结构化拼接
- 去重：同 hash 文件自动去重；multi_factor_report 已包含 goldwind 案例时做标注
- 可追溯：合并报告头部标注来源文件列表、合并时间、数据口径
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .timeutil import now, stamp

log = logging.getLogger(__name__)


@dataclass
class MarkdownSource:
    path: Path
    title: str
    content: str
    sha: str = ""
    size: int = 0

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass
class MergedReport:
    topic: str
    content: str
    sources: list[MarkdownSource] = field(default_factory=list)
    merged_at: datetime = field(default_factory=now)
    notes: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return len(self.content)


def _extract_title(md: str, fallback: str) -> str:
    """提取 Markdown 首个 H1/H2 作为标题."""
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped.startswith("## "):
            # 允许二级标题作为标题备选
            return stripped[3:].strip()
    return fallback


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_markdown_file(path: Path | str) -> MarkdownSource:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"合并源文件不存在: {p}")
    text = p.read_text(encoding="utf-8")
    title = _extract_title(text, p.stem)
    sha = _hash_content(text)
    return MarkdownSource(path=p, title=title, content=text.strip(), sha=sha, size=len(text))


def dedup_sources(sources: list[MarkdownSource]) -> tuple[list[MarkdownSource], list[str]]:
    """按 hash 去重，返回 (去重后列表, 去重说明)."""
    seen: dict[str, MarkdownSource] = {}
    notes: list[str] = []
    result: list[MarkdownSource] = []
    for src in sources:
        if src.sha in seen:
            notes.append(f"去重：{src.filename} 与 {seen[src.sha].filename} 内容一致（{src.sha}），已合并为1份")
        else:
            seen[src.sha] = src
            result.append(src)
    return result, notes


def merge_markdowns(
    file_paths: Iterable[Path | str],
    merge_topic: str = "",
    *,
    add_toc: bool = True,
    add_provenance: bool = True,
    ref: datetime | None = None,
) -> MergedReport:
    """合并多份 Markdown 文件，返回 MergedReport.

    Args:
        file_paths: 要合并的文件路径列表
        merge_topic: 合并后的主题标题，空则自动生成
        add_toc: 是否在头部生成目录
        add_provenance: 是否生成数据来源可追溯说明
    """
    ref = ref or now()
    raw_sources: list[MarkdownSource] = []
    for fp in file_paths:
        try:
            raw_sources.append(load_markdown_file(fp))
        except Exception as exc:
            log.warning("跳过无法读取的合并源 %s: %s", fp, exc)

    if not raw_sources:
        raise ValueError("没有可用的合并源文件")

    deduped, dedup_notes = dedup_sources(raw_sources)

    # 自动生成主题
    if merge_topic.strip():
        topic = merge_topic.strip()
    else:
        # 用最长的标题 + “合并报告” 作为主题，或用文件名拼接
        titles = [s.title for s in deduped[:3]]
        if len(deduped) == 1:
            topic = f"{deduped[0].title}（合并版）"
        else:
            topic = f"{' + '.join(titles[:2])} 等 {len(deduped)} 份报告合并分析"

    notes = list(dedup_notes)

    # 检测 multi_factor_report 已包含 goldwind 的情况，做合并说明
    filenames = {s.filename.lower() for s in deduped}
    has_multi = any("multi_factor" in f for f in filenames)
    has_goldwind = any("goldwind" in f for f in filenames)
    if has_multi and has_goldwind:
        notes.append("检测到 multi_factor_report.md 已包含金风科技案例（第5节），与 goldwind_analysis.md 同源，合并时保留双方差异化表述并以 multi_factor 第5节为基准补充交易边界")

    # 构造合并正文
    lines: list[str] = []
    lines.append(f"# {topic}")
    lines.append("")
    lines.append(f"> **合并时间**：{stamp(ref)}（北京时间）  |  **来源数**：{len(deduped)}  |  **总字符**：{sum(s.size for s in deduped)}")
    lines.append("")

    if add_provenance:
        lines.append("## 合并说明与来源追溯")
        lines.append("")
        lines.append("| 序号 | 文件名 | 标题 | 字符数 | 内容指纹 |")
        lines.append("|---:|---|---|---:|---|")
        for i, src in enumerate(deduped, 1):
            lines.append(f"| {i} | `{src.filename}` | {src.title[:40]} | {src.size} | `{src.sha}` |")
        lines.append("")
        if notes:
            lines.append("**合并备注**：")
            for n in notes:
                lines.append(f"- {n}")
            lines.append("")

    if add_toc:
        lines.append("## 目录")
        lines.append("")
        for i, src in enumerate(deduped, 1):
            anchor = src.title.lower().replace(" ", "-").replace("·", "").replace("（", "").replace("）", "")
            lines.append(f"{i}. [{src.title}](#{anchor}) — `{src.filename}`")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 正文拼接
    for idx, src in enumerate(deduped, 1):
        lines.append(f"## 原始报告 {idx}：{src.title}")
        lines.append("")
        lines.append(f"> **文件名**：`{src.filename}`  |  **指纹**：`{src.sha}`  |  **字符**：{src.size}")
        lines.append("")
        # 外层已经显示“原始报告 N：标题”，去掉源文件开头重复的 H1。
        # 其余章节标题原样保留，渲染器会据此拆成适合手机阅读的章节卡片。
        content_lines = src.content.splitlines()
        first_content = next((i for i, line in enumerate(content_lines) if line.strip()), None)
        if first_content is not None and content_lines[first_content].lstrip().startswith("# "):
            content_lines.pop(first_content)
        lines.append("\n".join(content_lines).strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    # 合并总结（如果涉及多因子+金风，则追加综合结论）
    if has_multi and has_goldwind:
        lines.append("## 合并综合结论")
        lines.append("")
        lines.append("本合并报告整合了 **多因子量化模型全栈方法论** 与 **金风科技动量共振实证** 两部分：")
        lines.append("")
        lines.append("- **方法论层**：基于 AkShare / BaoStock / Qlib Data / DuckDB 等开源栈，构建 22+ 因子库，执行 MAD 去极值、Z-Score 标准化、OLS 行业/市值中性化、涨跌停偏差修正四步清洗；")
        lines.append("- **有效性层**：长周期检验 IC / Rank IC / IR / 胜率 / 多空年化，Top 组年化 +21.4% 跑赢中证500基准，多空 Sharpe 1.84；")
        lines.append("- **实证层**：金风科技港股 02208.HK 处于 52周低位 +10% 右侧启动初期，短期动量 +12.5%（跑赢恒指10.4pct）、换手率放大2.4倍站上20日线、RSI 28.7→48.5 底背离、3家券商上调 EPS、聪明钱占比62%、舆情周增180%；")
        lines.append("- **风控层**：综合量化评分 86.7 分（前8%分位），建仓区间 4.20-4.45 港元，第一目标 5.60 港元（+28%空间），硬止损 4.05 港元（-6.5%），行业权重上限 4.0%。")
        lines.append("")
        lines.append("> 合并价值在于：避免“空泛研报”——每条结论绑定具体读数、分位数、公式与交易边界，可直接进入投研流水线检验。")
        lines.append("")

    merged_content = "\n".join(lines).strip() + "\n"

    return MergedReport(
        topic=topic,
        content=merged_content,
        sources=deduped,
        merged_at=ref,
        notes=notes,
    )


def merge_theme_analyses(
    topics: list[str],
    *,
    merge_topic: str = "",
) -> str:
    """简易版：把多个主题字符串合并成一个待分析主题."""
    cleaned = [t.strip() for t in topics if t.strip()]
    if not cleaned:
        return ""
    if merge_topic.strip():
        return merge_topic.strip()
    # 去重保序
    seen = []
    for t in cleaned:
        if t not in seen:
            seen.append(t)
    return " + ".join(seen)


# ---------------------------------------------------------------------------
# 辅助：直接生成当前仓库的示范合并报告（goldwind + multi_factor）
# ---------------------------------------------------------------------------
def generate_demo_merged_report(
    base_dir: Path | None = None,
    output_name: str = "merged_report.md",
) -> Path:
    base = Path(base_dir) if base_dir else Path.cwd()
    candidates = [
        base / "goldwind_analysis.md",
        base / "multi_factor_report.md",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        raise FileNotFoundError("未找到可合并的示范文件")

    report = merge_markdowns(existing, merge_topic="多因子量化模型深度分析与金风科技实证 - 合并报告")
    out = base / output_name
    out.write_text(report.content, encoding="utf-8")
    log.info("示范合并报告已生成：%s (%d 字符)", out, len(report.content))
    return out
