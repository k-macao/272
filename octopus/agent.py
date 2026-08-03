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
from .render import render_html, render_manual, render_manual_title, render_title
from .sources import REGISTRY
from .state import SeenStore
from .timeutil import now, stamp

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
