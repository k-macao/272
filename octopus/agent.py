"""抓取智能体主流程：并发抓十个源 -> 时间校验 -> 去重 -> 渲染 -> 推送."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .http import Http
from .models import Item, SourceResult
from .notify import PushPlus
from .render import (
    render_html,
    render_manual,
    render_manual_title,
    render_merge,
    render_merge_title,
    render_theme,
    render_theme_title,
    render_title,
)
from .sources import REGISTRY
from .state import SeenStore
from .timeutil import in_quiet_hours, now, stamp

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    total: int
    pushed: bool
    groups: list[tuple[SourceResult, list[Item]]]
    results: list[SourceResult]
    html: str
    title: str
    ref: datetime
    skipped: str = ""
    """跳过原因标记："quiet" = 本轮因夜间免打扰被整体跳过（未抓、未推、未记账）。"""

    analysis: object | None = None
    """主题因子分析结果（仅 push_theme 填充，其余流程为 None）。"""

    @property
    def failures(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]


class Agent:
    def __init__(self, config: Config, *, base_dir: Path | None = None) -> None:
        self.config = config
        self.base_dir = base_dir or Path.cwd()
        self.http = Http(timeout=config.timeout, retries=config.retries)
        state_path = self.base_dir / config.state_file
        self.seen = SeenStore(state_path)

    # ------------------------------------------------------------------
    def run_once(self, *, dry_run: bool = False, ref: datetime | None = None) -> RunReport:
        ref = ref or now()
        if in_quiet_hours(self.config.quiet_start, self.config.quiet_end, ref=ref):
            msg = (f"夜间免打扰（{self.config.quiet_start}–{self.config.quiet_end} 北京时间），"
                   f"当前 {stamp(ref)}")
            if dry_run:
                # dry-run 是人工主动预览，不受免打扰限制（本来也不会真推）
                log.info("=== %s；dry-run 照常抓取仅生成预览", msg)
            else:
                log.info("=== %s，本轮暂停抓取与推送，%s 起床", msg, self.config.quiet_end)
                return RunReport(
                    total=0,
                    pushed=False,
                    groups=[],
                    results=[],
                    html="",
                    title=f"免打扰暂停（至次日 {self.config.quiet_end}）",
                    ref=ref,
                    skipped="quiet",
                )
        log.info("=== 开始扫描 %s（窗口 %d 分钟，已记录 %d 条历史）",
                 stamp(ref), self.config.window_minutes, len(self.seen))

        results = self._collect_all(ref)

        # 按注册表顺序整理分组，并施加全局条数上限。
        # max_items_total <= 0 表示不限——所有通过时间校验的抓取内容
        # 全量塞进同一条推送，一条不漏。
        groups: list[tuple[SourceResult, list[Item]]] = []
        by_name = {r.source: r for r in results}
        budget = self.config.max_items_total
        capped = budget > 0
        for name in REGISTRY:
            result = by_name.get(name)
            if not result or not result.items:
                continue
            take = result.items[:budget] if capped else result.items
            if capped:
                budget -= len(take)
            if take:
                groups.append((result, take))

        total = sum(len(items) for _, items in groups)
        newest = _newest(groups)

        html = render_html(
            groups,
            total=total,
            window_minutes=self.config.window_minutes,
            ref=ref,
            failures=[r for r in results if not r.ok],
            degraded=[r for r in results if r.degraded],
        )
        title = render_title(total, ref, newest)

        pushed = False
        should_push = total > 0 or self.config.push_when_empty
        if should_push:
            pushed = self._push(title, html, dry_run=dry_run)
        else:
            log.info("本轮无新增且配置为静默，跳过推送")

        # 只有推成功才记账，避免推送失败导致内容永久丢失
        if pushed and total:
            for _, items in groups:
                self.seen.add_many(item.dedupe_key() for item in items)
            self.seen.save()
        elif not total:
            self.seen.save()  # 触发过期清理

        log.info("=== 扫描结束：新增 %d 条，推送 %s", total, "成功" if pushed else "未执行/失败")
        return RunReport(
            total=total,
            pushed=pushed,
            groups=groups,
            results=results,
            html=html,
            title=title,
            ref=ref,
        )

    # ------------------------------------------------------------------
    def _collect_all(self, ref: datetime) -> list[SourceResult]:
        disabled = set(self.config.disabled_sources)
        active = {n: c for n, c in REGISTRY.items() if n not in disabled}
        if disabled:
            log.info("已禁用的源：%s", ", ".join(sorted(disabled)))

        results: list[SourceResult] = []
        with ThreadPoolExecutor(max_workers=min(8, len(active) or 1)) as pool:
            futures = {}
            for name, cls in active.items():
                source = cls(self.http, self.config.for_source(name))
                futures[pool.submit(
                    source.run,
                    window_minutes=self.config.window_minutes,
                    seen=self.seen,
                    ref=ref,
                )] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 兜底，理论上 run 内部已捕获
                    cls = REGISTRY[name]
                    log.exception("源 %s 抛出未捕获异常", name)
                    results.append(
                        SourceResult(
                            source=name,
                            source_label=getattr(cls, "label", name),
                            ok=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
        return results

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 合并推送：多份 Markdown 合并 -> 渲染 -> 一对一推送
    # ------------------------------------------------------------------
    def _load_merge_sources(
        self, file_paths: list[str], merge_topic: str
    ) -> tuple[str, str, list[str], str, str]:
        from .merge import merge_markdowns

        if not file_paths:
            raise ValueError("未提供合并源文件")
        report = merge_markdowns(file_paths, merge_topic=merge_topic or "")
        # 可选 DeepSeek 提炼
        ai_summary, ai_model = ("", "")
        if self.config.deepseek_api_key:
            ai_summary, ai_model = self._refine_manual(report.topic, report.content)
        return report.topic, report.content, report.notes, ai_summary, ai_model

    def preview_merge(
        self,
        file_paths: list[str],
        merge_topic: str = "",
        *,
        ref: datetime | None = None,
        use_ai: bool = True,
    ) -> str:
        from .merge import merge_markdowns

        ref = ref or now()
        merged = merge_markdowns(file_paths, merge_topic=merge_topic, ref=ref)
        if use_ai:
            ai_summary, ai_model = self._refine_manual(merged.topic, merged.content)
        else:
            ai_summary, ai_model = "", ""
        return render_merge(
            merged.topic,
            merged.content,
            ref=ref,
            source_count=len(merged.sources),
            ai_summary=ai_summary,
            ai_model=ai_model,
        )

    def push_merge(
        self,
        file_paths: list[str],
        merge_topic: str = "",
        *,
        dry_run: bool = False,
        ref: datetime | None = None,
        use_ai: bool = True,
    ) -> RunReport:
        from .merge import merge_markdowns

        ref = ref or now()
        merged = merge_markdowns(file_paths, merge_topic=merge_topic, ref=ref)
        ai_summary, ai_model = ("", "")
        if use_ai:
            ai_summary, ai_model = self._refine_manual(merged.topic, merged.content) if use_ai else ("", "")

        html = render_merge(
            merged.topic,
            merged.content,
            ref=ref,
            source_count=len(merged.sources),
            ai_summary=ai_summary,
            ai_model=ai_model,
        )
        title = render_merge_title(merged.topic, ref, len(merged.sources))
        pushed = self._push(title, html, dry_run=dry_run, topics=[])
        log.info("=== 合并报告推送：%d 源 -> %s：%s", len(merged.sources), merged.topic, "成功" if pushed else "未执行/失败")
        return RunReport(
            total=len(merged.sources),
            pushed=pushed,
            groups=[],
            results=[],
            html=html,
            title=title,
            ref=ref,
        )

    def _refine_manual(self, topic: str, content: str) -> tuple[str, str]:
        """按需调用 DeepSeek 大模型 API 提炼主题内容。"""
        if not self.config.deepseek_api_key or not (content or "").strip():
            return "", ""
        from .ai import DeepSeekAI

        client = DeepSeekAI(
            self.config.deepseek_api_key,
            model=self.config.deepseek_model,
            http=self.http,
        )
        log.info("调用 DeepSeek API (%s) 提炼分类与摘要...", self.config.deepseek_model)
        ok, res = client.analyze(topic, content)
        if ok and res:
            log.info("DeepSeek API 提炼完成 (%d 字符)", len(res))
            return res, self.config.deepseek_model
        log.warning("DeepSeek API 提炼未成功: %s", res)
        return "", ""

    def preview_manual(
        self,
        topic: str,
        content: str,
        *,
        ref: datetime | None = None,
        use_ai: bool = True,
    ) -> str:
        """生成手动主题分析的预览 HTML 正文（可选带 DeepSeek AI 提炼）。"""
        ref = ref or now()
        ai_summary, ai_model = self._refine_manual(topic, content) if use_ai else ("", "")
        return render_manual(
            topic,
            content,
            ref=ref,
            ai_summary=ai_summary,
            ai_model=ai_model,
        )

    def push_manual(
        self,
        topic: str,
        content: str,
        *,
        dry_run: bool = False,
        ref: datetime | None = None,
        use_ai: bool = True,
    ) -> RunReport:
        """手动主题分析推送：人工录入内容直接渲染并推送。

        支持自动调用 DeepSeek 大模型进行提炼、分类或摘要（如配置了 DEEPSEEK_API_KEY）。
        恒为**一对一**：只推给 token 所属账号本人（PushPlus 个人推送），
        不携带群组 topic，与 config.pushplus_topics（一对多）互不影响。
        """
        ref = ref or now()
        ai_summary, ai_model = self._refine_manual(topic, content) if use_ai else ("", "")
        html = render_manual(
            topic,
            content,
            ref=ref,
            ai_summary=ai_summary,
            ai_model=ai_model,
        )
        title = render_manual_title(topic, ref)
        pushed = self._push(title, html, dry_run=dry_run, topics=[])
        log.info("=== 手动主题分析：推送 %s", "成功" if pushed else "未执行/失败")
        return RunReport(
            total=1 if (content or "").strip() else 0,
            pushed=pushed,
            groups=[],
            results=[],
            html=html,
            title=title,
            ref=ref,
        )

    # ------------------------------------------------------------------
    # 主题因子分析（一对一）：只输入主题，其余全自动
    # ------------------------------------------------------------------
    def _pipeline(self):
        """惰性构造主题分析流水线（避免抓取模式白白 import）。"""
        from .factor.pipeline import ThemePipeline

        return ThemePipeline(
            self.http,
            base_dir=self.base_dir,
            deepseek_api_key=self.config.deepseek_api_key,
            deepseek_model=self.config.deepseek_model,
            github_token=self.config.github_token,
            stock_top=self.config.factor_stock_top,
            kline_limit=self.config.factor_kline_limit,
            supervision_days=self.config.supervision_days,
            market_source=self.config.factor_market_source,
        )

    def analyze_theme(
        self,
        topic: str,
        *,
        ref: datetime | None = None,
        use_ai: bool = True,
    ):
        """跑一次主题分析，返回 ThemeAnalysis（不推送）。"""
        return self._pipeline().run(topic, ref=ref or now(), use_ai=use_ai)

    def preview_theme(
        self,
        topic: str,
        *,
        ref: datetime | None = None,
        use_ai: bool = True,
    ) -> str:
        """生成主题因子分析的预览 HTML 正文。"""
        ref = ref or now()
        analysis = self.analyze_theme(topic, ref=ref, use_ai=use_ai)
        return render_theme(analysis, ref=ref)

    def push_theme(
        self,
        topic: str,
        *,
        dry_run: bool = False,
        ref: datetime | None = None,
        use_ai: bool = True,
    ) -> RunReport:
        """主题因子分析推送：输入主题 -> 监管+因子分析 -> 一对一推送。

        与 push_manual 一样恒为**一对一**（PushPlus 个人推送，不带群组 topic）。
        """
        ref = ref or now()
        analysis = self.analyze_theme(topic, ref=ref, use_ai=use_ai)
        html = render_theme(analysis, ref=ref)
        title = render_theme_title(topic, analysis, ref=ref)
        pushed = self._push(title, html, dry_run=dry_run, topics=[])
        log.info("=== 主题因子分析：推送 %s", "成功" if pushed else "未执行/失败")
        report = RunReport(
            total=len(analysis.profiles),
            pushed=pushed,
            groups=[],
            results=[],
            html=html,
            title=title,
            ref=ref,
        )
        report.analysis = analysis  # type: ignore[attr-defined]
        return report

    # ------------------------------------------------------------------
    def _push(self, title: str, html: str, *, dry_run: bool, topics: list[str] | None = None) -> bool:
        if not self.config.pushplus_token:
            log.error("未配置 PUSHPLUS_TOKEN，无法推送")
            return False
        # topics=None 时跟随配置（一对多群组）；显式传 [] 则一对一推给自己
        topics_to_use = self.config.pushplus_topics if topics is None else topics
        pusher = PushPlus(
            self.http,
            self.config.pushplus_token,
            topics=topics_to_use,
        )
        return pusher.send(title, html, dry_run=dry_run)

    def close(self) -> None:
        self.http.close()


def _newest(groups: list[tuple[SourceResult, list[Item]]]) -> Item | None:
    """挑一条最有代表性的做标题：优先最新的盘中信号。"""
    best: Item | None = None
    for _, items in groups:
        for item in items:
            if best is None or (
                item.published_at and best.published_at
                and item.published_at > best.published_at
            ):
                best = item
    return best
