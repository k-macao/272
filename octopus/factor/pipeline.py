"""主题分析流水线：主题 -> 监管 + 因子 -> AI 报告。

完整链路：
    1. 主题解析：拆词，匹配东财概念/行业板块，定出分析标的（成分股 + 基准指数）
    2. 因子模型：从 GitHub 拉 microsoft/qlib 的 Alpha158 定义（qlib_repo）
    3. 因子计算：在真实日线上求值（expr），压缩成六维画像（scoring）
    4. 市场监督管理：抓取监管事件、评估监管风险（supervision）
    5. AI 解读：把结构化事实喂给大模型写报告；没有 Key 时用规则化解读兜底
    6. 合规审查：对最终文本做违规表述检测与中性化改写（compliance）

每一步失败都单独降级并如实标注，绝不让整条链路因为一个环节挂掉而空手而归。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..http import FetchError, Http
from ..timeutil import now, stamp
from . import compliance
from .expr import FactorEngine
from .market import (
    BENCHMARKS,
    INDEX_HINTS,
    Board,
    Instrument,
    MarketData,
    MarketSnapshot,
    data_freshness,
    tokenize,
)
from .qlib_repo import CORE_FACTORS, FactorModel, QlibFactorRepo
from .scoring import FactorProfile, build_profile, cross_section
from .supervision import SupervisionReport, SupervisionSource, policy_context

log = logging.getLogger(__name__)

#: 计算因子所需的最少 K 线根数（60 日窗口 + 缓冲）
MIN_BARS = 70
DEFAULT_KLINE_LIMIT = 250


@dataclass
class ThemeAnalysis:
    """一次主题分析的全部产出。"""

    topic: str
    ref: datetime
    market: MarketSnapshot
    model: FactorModel
    profiles: list[FactorProfile] = field(default_factory=list)
    benchmark_profiles: list[FactorProfile] = field(default_factory=list)
    supervision: SupervisionReport = field(default_factory=SupervisionReport)
    ai_report: str = ""
    ai_model: str = ""
    ai_error: str = ""
    compliance_result: compliance.ComplianceResult | None = None
    notes: list[str] = field(default_factory=list)     # 降级/口径说明，如实展示

    @property
    def data_date(self) -> date | None:
        return self.market.data_date

    @property
    def used_ai(self) -> bool:
        return bool(self.ai_model)

    @property
    def all_profiles(self) -> list[FactorProfile]:
        return self.benchmark_profiles + self.profiles

    def ranking(self) -> list[tuple[str, float]]:
        return cross_section(self.profiles)


class ThemePipeline:
    """把主题变成一份可推送的分析报告。"""

    def __init__(
        self,
        http: Http,
        *,
        base_dir: Path | None = None,
        deepseek_api_key: str = "",
        deepseek_model: str = "deepseek-v4-flash",
        github_token: str = "",
        stock_top: int = 6,
        kline_limit: int = DEFAULT_KLINE_LIMIT,
        supervision_days: int = 30,
    ) -> None:
        self.http = http
        self.base_dir = base_dir or Path.cwd()
        self.deepseek_api_key = (deepseek_api_key or "").strip()
        self.deepseek_model = (deepseek_model or "deepseek-v4-flash").strip()
        self.stock_top = max(1, stock_top)
        self.kline_limit = max(MIN_BARS, kline_limit)
        self.market = MarketData(http)
        self.repo = QlibFactorRepo(
            http, cache_dir=self.base_dir / "state" / "factors", token=github_token
        )
        self.supervision = SupervisionSource(http, window_days=supervision_days)

    # ------------------------------------------------------------------
    def run(self, topic: str, *, ref: datetime | None = None, use_ai: bool = True) -> ThemeAnalysis:
        ref = ref or now()
        topic = (topic or "").strip()
        log.info("=== 主题分析开始：%s", topic or "（未指定主题）")

        snapshot = MarketSnapshot(topic=topic)
        analysis = ThemeAnalysis(topic=topic, ref=ref, market=snapshot, model=FactorModel())

        # --- 1. 因子模型（GitHub）与行情、监管并发拉取 --------------------
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_model = pool.submit(self.repo.load)
            f_market = pool.submit(self._collect_market, topic, ref)
            f_super = pool.submit(self._collect_supervision, topic, ref)

            analysis.model = f_model.result()
            analysis.market = snapshot = f_market.result()
            analysis.supervision = f_super.result()

        if analysis.model.degraded:
            analysis.notes.append(f"因子模型：{analysis.model.degraded}")
        analysis.notes.extend(snapshot.errors)

        # --- 2. 计算因子 -------------------------------------------------
        factors = self._select_factors(analysis.model)
        if not factors:
            analysis.notes.append("因子模型为空，跳过因子计算")
        else:
            analysis.benchmark_profiles = [
                self._profile(inst, factors) for inst in snapshot.benchmarks
            ]
            analysis.profiles = [self._profile(inst, factors) for inst in snapshot.stocks]

        # 监管事件与分析标的的交叉：板块内个股是否正被监管关注。
        # 抓取时还不知道会选中哪些个股（与行情并发），所以在这里回填 focus：
        # 命中股票代码，或标题/公司名命中主题关键词，都算"与本主题直接相关"。
        codes = {inst.code for inst in snapshot.stocks}
        focus = analysis.supervision.for_codes(codes)
        seen_ids = {id(e) for e in focus}
        for event in analysis.supervision.for_keywords(tokenize(topic)):
            if id(event) not in seen_ids:
                focus.append(event)
                seen_ids.add(id(event))
        focus.sort(key=lambda e: (-e.severity, -e.published_at.timestamp()))
        analysis.supervision.focus = focus
        if focus:
            analysis.notes.append(
                f"分析标的/主题涉及 {len(focus)} 条监管事件，已在报告中标注"
            )

        # --- 3. AI 解读（无 Key 时规则化兜底）-----------------------------
        facts = build_facts(analysis)
        if use_ai and self.deepseek_api_key:
            ok, text = self._ask_ai(topic, facts)
            if ok:
                analysis.ai_report = text
                analysis.ai_model = self.deepseek_model
            else:
                analysis.ai_error = text
                analysis.notes.append(f"大模型解读不可用（{text[:60]}），已改用规则化解读")
                analysis.ai_report = rule_based_report(analysis)
        else:
            if use_ai and not self.deepseek_api_key:
                analysis.notes.append("未配置 DEEPSEEK_API_KEY，使用内置规则化解读")
            analysis.ai_report = rule_based_report(analysis)

        # --- 4. 合规审查 ---------------------------------------------------
        result = compliance.review(analysis.ai_report)
        analysis.ai_report = result.text
        analysis.compliance_result = result
        if not result.clean:
            log.info("合规审查改写 %d 处", result.fixed_count)

        log.info(
            "=== 主题分析完成：标的 %d 个，因子 %d 个，监管事件 %d 条",
            len(analysis.profiles),
            len(factors),
            len(analysis.supervision.events),
        )
        return analysis

    # ------------------------------------------------------------------
    def _collect_market(self, topic: str, ref: datetime) -> MarketSnapshot:
        snapshot = MarketSnapshot(topic=topic)

        # 基准指数：主题里点名了就只取那一个，否则取上证 + 创业板
        wanted = _hinted_indices(topic)
        for secid, name, _ in BENCHMARKS:
            if secid not in wanted:
                continue
            try:
                snapshot.benchmarks.append(
                    self.market.load_instrument(
                        secid, name, limit=self.kline_limit, is_index=True
                    )
                )
            except FetchError as exc:
                snapshot.errors.append(f"指数 {name} 行情获取失败：{exc}")
                log.warning("指数 %s 行情失败：%s", name, exc)

        # 板块匹配 -> 成分股；匹配不到则降级为全市场成交额前列
        members: list[tuple[str, str, dict]] = []
        try:
            board, candidates = self.market.match_board(topic)
            snapshot.board = board
            snapshot.boards_considered = candidates
            if board:
                members = self.market.board_members(board.code, top=self.stock_top)
                snapshot.universe_note = (
                    f"主题命中「{board.name}」{board.kind}板块"
                    f"（关键词：{board.matched_by}），取板块内成交额前 {len(members)} 只个股"
                )
        except FetchError as exc:
            snapshot.errors.append(f"板块匹配失败：{exc}")
            log.warning("板块匹配失败：%s", exc)

        if not members:
            try:
                members = self.market.top_amount_stocks(top=self.stock_top)
                snapshot.universe_note = (
                    f"未匹配到对应板块，降级为全市场成交额前 {len(members)} 只个股"
                    "（分析口径已相应放宽）"
                )
            except FetchError as exc:
                snapshot.errors.append(f"个股列表获取失败：{exc}")
                log.warning("个股列表获取失败：%s", exc)

        for secid, name, quote in members:
            try:
                snapshot.stocks.append(
                    self.market.load_instrument(
                        secid, name, limit=self.kline_limit, quote=quote
                    )
                )
            except FetchError as exc:
                log.warning("个股 %s 行情失败：%s", name, exc)

        days = [i.last_day for i in snapshot.all_instruments if i.last_day]
        snapshot.data_date = max(days) if days else None
        if not snapshot.all_instruments:
            snapshot.errors.append("未能获取任何行情数据，因子分析无法进行")
        return snapshot

    # ------------------------------------------------------------------
    def _collect_supervision(self, topic: str, ref: datetime) -> SupervisionReport:
        """抓窗口内**全部**监管类公告。

        不在这里按主题关键词过滤：抓取与行情是并发的，此刻还不知道会选中
        哪些个股，过早过滤会把"分析标的正好被问询"这种最关键的信息漏掉。
        相关性判定统一放到 run() 里回填 focus。
        """
        try:
            return self.supervision.collect(ref=ref)
        except Exception as exc:  # noqa: BLE001 - 监管源挂掉不该拖垮整份报告
            log.warning("监管动态抓取异常：%s", exc)
            report = SupervisionReport()
            report.error = f"{type(exc).__name__}: {exc}"
            return report

    # ------------------------------------------------------------------
    def _select_factors(self, model: FactorModel) -> list[tuple[str, str]]:
        """从 158 个因子里挑核心子集参与计算（全算耗时且信息冗余）。"""
        table = {f.name: f.expr for f in model.factors}
        picked = [(name, table[name]) for name in CORE_FACTORS if name in table]
        if not picked:  # 因子命名意外变化时，退而取前 60 个
            picked = [(f.name, f.expr) for f in model.factors[:60]]
        return picked

    def _profile(self, inst: Instrument, factors: list[tuple[str, str]]) -> FactorProfile:
        if len(inst.bars) < MIN_BARS:
            profile = FactorProfile(name=inst.name, code=inst.code, total=len(factors))
            profile.dimensions = []
            log.info("%s 仅 %d 根K线，不足以计算因子", inst.name, len(inst.bars))
            return profile
        engine = FactorEngine(inst.fields())
        values = engine.evaluate_many(factors)
        inst.factors = values
        profile = build_profile(inst.name, inst.code, values)
        inst.scores = {
            d.key: d.score for d in profile.dimensions if d.score is not None
        }
        return profile

    # ------------------------------------------------------------------
    def _ask_ai(self, topic: str, facts: str) -> tuple[bool, str]:
        from ..ai import DeepSeekAI

        client = DeepSeekAI(self.deepseek_api_key, model=self.deepseek_model, http=self.http)
        log.info("调用 DeepSeek (%s) 生成因子分析报告...", self.deepseek_model)
        return client.analyze_theme(topic, facts)


# ---------------------------------------------------------------------------
def _hinted_indices(topic: str) -> set[str]:
    """主题点名了哪些宽基指数；没点名就用上证 + 创业板做背景。"""
    hits = {secid for words, secid in INDEX_HINTS if any(w in (topic or "") for w in words)}
    return hits or {"1.000001", "0.399006"}


# ---------------------------------------------------------------------------
def build_facts(analysis: ThemeAnalysis) -> str:
    """把结构化分析结果整理成给大模型的事实清单。

    刻意写成"事实 + 数值"的紧凑格式：大模型只负责解读与组织语言，
    不负责编造数据 —— 所有数字都来自这里。
    """
    lines: list[str] = []
    a = analysis
    lines.append(f"# 分析主题：{a.topic or '（未指定）'}")
    lines.append(f"分析时间：{stamp(a.ref)}（北京时间）")
    lines.append(f"行情数据截至：{data_freshness(a.data_date, ref=a.ref)}")
    lines.append(f"因子模型来源：{a.model.provenance}，共 {len(a.model.factors)} 个因子定义")

    # --- 标的口径 -------------------------------------------------------
    lines.append("")
    lines.append("## 一、分析标的")
    if a.market.board:
        b = a.market.board
        lines.append(
            f"命中板块：{b.name}（{b.kind}，代码 {b.code}），"
            f"板块涨跌幅 {b.change:+.2f}%，主力净流入 {b.main_inflow / 1e8:+.2f} 亿元"
            + (f"，领涨股 {b.leader}" if b.leader else "")
        )
    if a.market.boards_considered and len(a.market.boards_considered) > 1:
        others = "、".join(f"{x.name}({x.change:+.2f}%)" for x in a.market.boards_considered[1:4])
        lines.append(f"其他候选板块：{others}")
    if a.market.universe_note:
        lines.append(f"标的选取口径：{a.market.universe_note}")

    # --- 因子结果 -------------------------------------------------------
    lines.append("")
    lines.append("## 二、量化因子计算结果（Alpha158 核心因子）")
    for profile in a.all_profiles:
        if not profile.dimensions:
            lines.append(f"- {profile.name}：历史数据不足，未计算因子")
            continue
        composite = "—" if profile.composite is None else f"{profile.composite:.1f}"
        lines.append(f"### {profile.name}（{profile.code}）综合因子分 {composite}/100，{profile.stance}")
        for dim in profile.dimensions:
            score = "—" if dim.score is None else f"{dim.score:.0f}"
            lines.append(f"  - {dim.label}[{score}分/{dim.level}]：{dim.detail}")
        # 附上关键原始因子值，便于大模型引用具体数字
        evidence = [e for dim in profile.dimensions for e in dim.evidence][:8]
        if evidence:
            lines.append(f"  - 关键因子值：{'；'.join(evidence)}")

    ranking = a.ranking()
    if len(ranking) > 1:
        board_avg = sum(s for _, s in ranking) / len(ranking)
        lines.append("")
        lines.append(
            "横截面分布（仅供观察，不构成推荐）："
            + "，".join(f"{n} {s:.0f}分" for n, s in ranking)
            + f"；样本均值 {board_avg:.1f} 分"
        )

    # --- 监管 -----------------------------------------------------------
    lines.append("")
    lines.append("## 三、A股市场监督管理视角")
    sup = a.supervision
    lines.append(sup.summary_line())
    policy = policy_context(a.topic)
    if policy:
        lines.append(f"主题涉及监管政策关键词：{'、'.join(policy)}，属于政策敏感领域，解读需格外审慎")
    related = sup.focus
    if related:
        lines.append("与分析标的/主题直接相关的监管事件（必须在报告中提及）：")
        for event in related[:6]:
            lines.append(
                f"  - [{event.category}] {event.title}"
                f"（{event.published_at:%Y-%m-%d %H:%M}，严重度 {event.severity}）"
            )
    focus_ids = {id(e) for e in related}
    other_events = [e for e in sup.events if id(e) not in focus_ids][:6]
    if other_events:
        lines.append("同期市场其他监管事件（背景参考，不必逐条展开）：")
        for event in other_events:
            lines.append(f"  - [{event.category}] {event.title}（{event.published_at:%Y-%m-%d}）")

    # --- 数据可靠性 ------------------------------------------------------
    if a.notes:
        lines.append("")
        lines.append("## 四、数据与口径说明（必须在报告中如实反映）")
        for note in a.notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
def rule_based_report(analysis: ThemeAnalysis) -> str:
    """没有大模型时的规则化解读 —— 保证任何情况下都有一份像样的报告。

    刻意不写成"AI 生成"，措辞与 AI 版保持同样的合规口径。
    """
    a = analysis
    out: list[str] = []

    out.append("【主题分类】")
    board = a.market.board
    if board:
        out.append(
            f"{board.kind}主题 · {board.name}（{board.code}）"
            f"｜板块最新涨跌 {board.change:+.2f}%｜关键词：{board.matched_by}"
        )
    else:
        out.append(f"通用市场主题 · {a.topic or '未指定'}｜未匹配到具体板块，按全市场口径分析")

    out.append("")
    out.append("【核心结论】")
    profiles = [p for p in a.profiles if p.composite is not None]
    bench = [p for p in a.benchmark_profiles if p.composite is not None]
    if profiles:
        avg = sum(p.composite for p in profiles) / len(profiles)  # type: ignore[misc]
        bench_txt = ""
        if bench:
            bavg = sum(p.composite for p in bench) / len(bench)  # type: ignore[misc]
            gap = avg - bavg
            bench_txt = (
                f"，同期基准指数均值 {bavg:.1f} 分，"
                f"样本{'高于' if gap > 0 else '低于'}基准 {abs(gap):.1f} 分"
            )
        out.append(
            f"基于 microsoft/qlib Alpha158 因子模型对 {len(profiles)} 只标的的计算，"
            f"综合因子分均值 {avg:.1f}/100{bench_txt}。"
            f"{_stance_sentence(avg)}"
        )
    elif bench:
        bavg = sum(p.composite for p in bench) / len(bench)  # type: ignore[misc]
        out.append(
            f"个股样本因子数据不足，仅计算了基准指数：综合因子分均值 {bavg:.1f}/100。"
            f"{_stance_sentence(bavg)}"
        )
    else:
        out.append("行情数据不足，未能完成因子计算，本次不给出因子层面的结论。")

    sup = a.supervision
    out.append(
        f"监管维度：{sup.summary_line()}。"
        + ("相关事件已在下方列出，参与该主题需将合规风险计入。" if sup.events else "")
    )

    # 逐维度明细由推送里的「量化因子评分」卡片呈现，这里只做提炼，
    # 避免同一份推送把六个维度讲两遍。
    out.append("")
    out.append("【因子要点】")
    for profile in a.all_profiles:
        if not profile.dimensions:
            out.append(f"· {profile.name}：历史行情不足 {MIN_BARS} 根K线，未计算因子")
            continue
        scored = [d for d in profile.dimensions if d.score is not None]
        if not scored:
            out.append(f"· {profile.name}：各维度均缺少足够数据")
            continue
        best = max(scored, key=lambda d: d.score)      # type: ignore[arg-type,return-value]
        worst = min(scored, key=lambda d: d.score)     # type: ignore[arg-type,return-value]
        composite = "—" if profile.composite is None else f"{profile.composite:.1f}"
        line = f"· {profile.name}（{profile.code}）综合 {composite} 分：{profile.stance}"
        if best.key != worst.key:
            line += (
                f"；最强项为{best.label}（{best.score:.0f}），"
                f"最弱项为{worst.label}（{worst.score:.0f}）"
            )
        out.append(line)
        # 只展开最关键的一条判读，其余交给评分卡
        out.append(f"    {best.label}：{best.detail}")
        if best.key != worst.key:
            out.append(f"    {worst.label}：{worst.detail}")

    ranking = a.ranking()
    if len(ranking) > 1:
        out.append("")
        out.append("【横截面分布】（仅呈现因子分布，不构成任何推荐）")
        out.append("  " + " ｜ ".join(f"{n} {s:.0f}" for n, s in ranking))
        spread = ranking[0][1] - ranking[-1][1]
        out.append(
            f"  样本内最高与最低相差 {spread:.0f} 分，"
            + ("个股分化明显，板块内部走势并不同步" if spread >= 30 else "个股表现相对接近")
        )

    out.append("")
    out.append("【监管与合规提示】")
    if sup.events:
        related = sup.focus
        if related:
            out.append(f"与分析标的/主题直接相关（风险等级：{sup.risk_level}）：")
            for event in related[:5]:
                out.append(
                    f"  · [{event.category}] {event.title}（{event.published_at:%m-%d %H:%M}）"
                )
        else:
            out.append(f"  · 分析标的近 {sup.window_days} 天未涉及监管事件")
        focus_ids = {id(e) for e in related}
        others = [e for e in sup.events if id(e) not in focus_ids][:5]
        if others:
            out.append("同期市场监管动态（背景参考）：")
            for event in others:
                out.append(f"  · [{event.category}] {event.title}（{event.published_at:%m-%d}）")
    else:
        out.append(f"  · 近 {sup.window_days} 天未检出监管类公告（已扫描 {sup.scanned} 条）")
    policy = policy_context(a.topic)
    if policy:
        out.append(f"  · 主题涉及政策敏感词：{'、'.join(policy)}，请以监管部门正式发布口径为准")

    out.append("")
    out.append("【风险提示】")
    out.append("  · 因子模型基于历史价量数据的统计规律，不包含基本面与突发事件信息，历史表现不预示未来")
    out.append("  · 六维评分为线性映射后的相对读数，高分仅代表历史统计特征偏正面，同时意味着累计涨幅已较大")
    if a.market.board:
        out.append("  · 样本为板块内成交额前列个股，不代表板块全貌，存在样本选择偏差")
    if a.data_date is not None:
        out.append(f"  · 行情数据截至 {a.data_date:%Y-%m-%d}，若期间发生重大事件，读数会滞后")
    out.append("  · 监管事件依赖公开公告抓取，可能存在延迟或遗漏，请以交易所与证监会正式公告为准")

    out.append("")
    out.append("【数据与口径】")
    out.append(f"  · 因子模型：{a.model.provenance}")
    out.append(f"  · 行情数据截至：{data_freshness(a.data_date, ref=a.ref)}")
    if a.market.universe_note:
        out.append(f"  · 标的口径：{a.market.universe_note}")
    for note in a.notes:
        out.append(f"  · {note}")

    return "\n".join(out)


def _stance_sentence(score: float) -> str:
    if score >= 70:
        return "多数维度读数偏正面，但高读数同时意味着价格已有较大累计涨幅，需注意回撤风险。"
    if score >= 55:
        return "整体读数略偏正面，各维度分化不大，属于中性偏强的结构。"
    if score > 45:
        return "多空信号交织，因子层面未形成一致方向，属于典型的震荡结构。"
    if score > 30:
        return "整体读数略偏负面，需关注趋势与量能能否修复。"
    return "多数维度读数偏弱，因子层面尚未出现改善迹象。"
