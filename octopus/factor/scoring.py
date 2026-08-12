"""因子解读：把 158 个原始因子值压缩成人能读懂的几个维度。

Alpha158 的原始值（如 MA20=0.9539、CORR20=-0.076）直接甩给读者毫无意义，
也不适合直接喂给大模型 —— token 多、信噪比低。这里做三件事：

1. **归一**：把与量纲相关的因子还原成直觉量（MA20=0.95 -> 价格高于20日均线 4.8%）
2. **聚合**：按动量/趋势/波动/量能/量价/强弱六个维度打分（0-100）
3. **表述**：给每个维度生成一句中性的、可直接进报告的解读

打分刻意做得保守：任何维度缺数据就返回 None 而不是给个中间值，
报告里如实写"数据不足"，绝不用臆造的分数撑场面。
"""

from __future__ import annotations

from dataclasses import dataclass, field

Value = float | None


@dataclass
class Dimension:
    """一个分析维度的得分与解读。"""

    key: str
    label: str
    score: float | None          # 0-100，None = 数据不足
    detail: str                  # 一句话解读
    evidence: list[str] = field(default_factory=list)   # 支撑该结论的因子明细

    @property
    def level(self) -> str:
        if self.score is None:
            return "数据不足"
        if self.score >= 70:
            return "偏强"
        if self.score >= 55:
            return "中性偏强"
        if self.score > 45:
            return "中性"
        if self.score > 30:
            return "中性偏弱"
        return "偏弱"


@dataclass
class FactorProfile:
    """一个标的的完整因子画像。"""

    name: str
    code: str
    dimensions: list[Dimension] = field(default_factory=list)
    composite: float | None = None
    covered: int = 0             # 成功算出的因子数
    total: int = 0               # 尝试计算的因子数

    def dim(self, key: str) -> Dimension | None:
        for d in self.dimensions:
            if d.key == key:
                return d
        return None

    @property
    def coverage(self) -> float:
        return (self.covered / self.total * 100.0) if self.total else 0.0

    @property
    def stance(self) -> str:
        """综合结论的中性表述。"""
        if self.composite is None:
            return "因子数据不足，不做方向性判断"
        if self.composite >= 70:
            return "多数因子偏正面"
        if self.composite >= 55:
            return "因子整体略偏正面"
        if self.composite > 45:
            return "因子多空信号交织"
        if self.composite > 30:
            return "因子整体略偏负面"
        return "多数因子偏负面"


# ---------------------------------------------------------------------------
def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: Value, center: float, span: float, *, invert: bool = False) -> float | None:
    """把一个因子值线性映射到 0-100，center 对应 50 分。

    span 是"到 100 分（或 0 分）需要偏离多少"，超出部分裁剪。
    """
    if value is None:
        return None
    if span == 0:
        return 50.0
    ratio = (value - center) / span
    if invert:
        ratio = -ratio
    return _clip(50.0 + ratio * 50.0)


def _avg(values: list[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return sum(real) / len(real) if real else None


def _pct(value: Value, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _fmt(value: Value, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
def build_profile(name: str, code: str, values: dict[str, Value]) -> FactorProfile:
    """把原始因子值映射成六维画像。"""
    total = len(values)
    covered = sum(1 for v in values.values() if v is not None)
    profile = FactorProfile(name=name, code=code, covered=covered, total=total)

    profile.dimensions = [
        _momentum(values),
        _trend(values),
        _volatility(values),
        _volume(values),
        _pricevolume(values),
        _strength(values),
    ]

    scores = [d.score for d in profile.dimensions if d.score is not None]
    profile.composite = round(sum(scores) / len(scores), 1) if scores else None
    return profile


# --- 各维度 ----------------------------------------------------------------
def _momentum(v: dict[str, Value]) -> Dimension:
    """动量：ROC 是 N日前价/最新价，<1 表示上涨，所以要反向。"""
    parts: list[float | None] = []
    evidence: list[str] = []
    rets: dict[int, float] = {}
    for window, span in ((5, 0.06), (10, 0.09), (20, 0.14), (60, 0.25)):
        roc = v.get(f"ROC{window}")
        if roc is None or roc <= 0:
            continue
        change = 1.0 / roc - 1.0        # 区间实际涨跌幅
        rets[window] = change
        parts.append(_scale(change, 0.0, span))
        evidence.append(f"近{window}日涨跌幅 {change * 100:+.2f}%（ROC{window}={roc:.4f}）")

    score = _avg(parts)
    if score is None:
        return Dimension("momentum", "价格动量", None, "缺少足够的历史行情，动量因子无法计算", evidence)

    if rets:
        short = rets.get(5)
        long = rets.get(60)
        positives = sorted(w for w, r in rets.items() if r > 0)
        negatives = sorted(w for w, r in rets.items() if r <= 0)
        if not negatives:
            shape = "各观察周期动量均为正，动量呈延续状态"
        elif not positives:
            shape = "各观察周期动量均为负，动量持续偏弱"
        elif short is not None and long is not None and short > 0 and long < 0:
            shape = "长周期仍为负、短周期已转正，属于下跌后的修复形态"
        elif short is not None and long is not None and short < 0 and long > 0:
            shape = "长周期为正但短周期回落，动量出现衰减"
        else:
            shape = (
                f"动量方向分化：{'/'.join(str(w) for w in positives)}日为正，"
                f"{'/'.join(str(w) for w in negatives)}日为负"
            )
        detail = f"{shape}。" + "；".join(
            f"近{w}日{r * 100:+.2f}%" for w, r in sorted(rets.items())
        )
    else:
        detail = "动量因子数据有限"
    return Dimension("momentum", "价格动量", round(score, 1), detail, evidence)


def _trend(v: dict[str, Value]) -> Dimension:
    """趋势：均线偏离 + 回归斜率 + 趋势线性度。"""
    parts: list[float | None] = []
    evidence: list[str] = []

    ma_bias: dict[int, float] = {}
    for window, span in ((5, 0.04), (10, 0.06), (20, 0.09), (60, 0.16)):
        ma = v.get(f"MA{window}")
        if ma is None or ma <= 0:
            continue
        bias = 1.0 / ma - 1.0           # 价格相对均线的偏离
        ma_bias[window] = bias
        parts.append(_scale(bias, 0.0, span))
        evidence.append(f"价格相对{window}日均线 {bias * 100:+.2f}%（MA{window}={ma:.4f}）")

    beta20 = v.get("BETA20")
    if beta20 is not None:
        parts.append(_scale(beta20, 0.0, 0.004))
        evidence.append(f"20日回归斜率 BETA20={beta20:.5f}")
    rsqr20 = v.get("RSQR20")
    if rsqr20 is not None:
        evidence.append(f"20日趋势线性度 R²={rsqr20:.3f}")
    resi20 = v.get("RESI20")
    if resi20 is not None:
        evidence.append(f"偏离回归线 {resi20 * 100:+.2f}%（RESI20）")

    score = _avg(parts)
    if score is None:
        return Dimension("trend", "趋势结构", None, "均线与回归类因子数据不足", evidence)

    above = [w for w, b in ma_bias.items() if b > 0]
    below = [w for w, b in ma_bias.items() if b <= 0]
    if above and not below:
        pos = f"价格站上全部观察均线（{'/'.join(str(w) for w in sorted(above))}日）"
    elif below and not above:
        pos = f"价格位于全部观察均线之下（{'/'.join(str(w) for w in sorted(below))}日）"
    elif above:
        pos = (
            f"价格站上 {'/'.join(str(w) for w in sorted(above))} 日均线，"
            f"仍低于 {'/'.join(str(w) for w in sorted(below))} 日均线"
        )
    else:
        pos = "均线位置数据不完整"

    quality = ""
    if rsqr20 is not None:
        if rsqr20 >= 0.7:
            quality = "，20日趋势线性度较高，走势相对连贯"
        elif rsqr20 <= 0.3:
            quality = "，20日趋势线性度偏低，走势以震荡为主"
    return Dimension("trend", "趋势结构", round(score, 1), pos + quality, evidence)


def _volatility(v: dict[str, Value]) -> Dimension:
    """波动与位置：波动率越低越"稳"，价格分位越高越强势。

    这个维度不做多空判断，只描述"波动大不大、位置高不高"，
    因此得分含义是"结构稳健度"，不是"看多程度"。
    """
    parts: list[float | None] = []
    evidence: list[str] = []

    std20 = v.get("STD20")
    if std20 is not None:
        parts.append(_scale(std20, 0.03, 0.03, invert=True))  # 波动越小得分越高
        evidence.append(f"20日波动率 {std20 * 100:.2f}%（STD20）")
    std60 = v.get("STD60")
    if std60 is not None:
        evidence.append(f"60日波动率 {std60 * 100:.2f}%（STD60）")

    rank20 = v.get("RANK20")
    if rank20 is not None:
        parts.append(_clip(rank20 * 100.0))
        evidence.append(f"价格处于20日 {rank20 * 100:.0f}% 分位（RANK20）")
    rank60 = v.get("RANK60")
    if rank60 is not None:
        evidence.append(f"价格处于60日 {rank60 * 100:.0f}% 分位（RANK60）")

    rsv20 = v.get("RSV20")
    if rsv20 is not None:
        parts.append(_clip(rsv20 * 100.0))
        evidence.append(f"20日高低区间位置 {rsv20 * 100:.0f}%（RSV20）")

    max20 = v.get("MAX20")
    min20 = v.get("MIN20")
    if max20 is not None and max20 > 0:
        evidence.append(f"距20日高点 {(max20 - 1) * 100:.2f}%")
    if min20 is not None and min20 > 0:
        evidence.append(f"距20日低点 {(1 - min20) * 100:.2f}%")

    score = _avg(parts)
    if score is None:
        return Dimension("volatility", "波动与位置", None, "波动率与分位因子数据不足", evidence)

    bits: list[str] = []
    if std20 is not None:
        if std20 >= 0.05:
            bits.append(f"20日波动率 {std20 * 100:.2f}%，处于较高水平，价格摆动幅度大")
        elif std20 <= 0.02:
            bits.append(f"20日波动率 {std20 * 100:.2f}%，处于较低水平，价格相对平稳")
        else:
            bits.append(f"20日波动率 {std20 * 100:.2f}%，处于中等水平")
    if rsv20 is not None:
        if rsv20 >= 0.8:
            bits.append("当前价格接近20日区间上沿")
        elif rsv20 <= 0.2:
            bits.append("当前价格接近20日区间下沿")
        else:
            bits.append(f"当前价格位于20日区间 {rsv20 * 100:.0f}% 位置")
    return Dimension("volatility", "波动与位置", round(score, 1), "；".join(bits), evidence)


def _volume(v: dict[str, Value]) -> Dimension:
    """量能：VMA 是均量/最新量，<1 表示当前放量。"""
    parts: list[float | None] = []
    evidence: list[str] = []

    ratios: dict[int, float] = {}
    for window in (5, 20):
        vma = v.get(f"VMA{window}")
        if vma is None or vma <= 0:
            continue
        ratio = 1.0 / vma               # 最新量 / N日均量
        ratios[window] = ratio
        parts.append(_scale(ratio, 1.0, 1.0))
        evidence.append(f"最新成交量 / {window}日均量 = {ratio:.2f}（VMA{window}={vma:.3f}）")

    vsumd = v.get("VSUMD20")
    if vsumd is not None:
        parts.append(_scale(vsumd, 0.0, 0.5))
        evidence.append(f"20日量能净变化 VSUMD20={vsumd:+.3f}")
    vstd20 = v.get("VSTD20")
    if vstd20 is not None:
        evidence.append(f"20日量能波动 VSTD20={vstd20:.3f}")
    wvma20 = v.get("WVMA20")
    if wvma20 is not None:
        evidence.append(f"量加权价波动 WVMA20={wvma20:.3f}")

    score = _avg(parts)
    if score is None:
        return Dimension("volume", "量能结构", None, "成交量类因子数据不足", evidence)

    bits: list[str] = []
    r20 = ratios.get(20)
    if r20 is not None:
        if r20 >= 1.5:
            bits.append(f"最新成交量为20日均量的 {r20:.2f} 倍，显著放量")
        elif r20 >= 1.1:
            bits.append(f"最新成交量为20日均量的 {r20:.2f} 倍，温和放量")
        elif r20 <= 0.7:
            bits.append(f"最新成交量仅为20日均量的 {r20:.2f} 倍，明显缩量")
        else:
            bits.append(f"最新成交量为20日均量的 {r20:.2f} 倍，量能基本持平")
    if vsumd is not None:
        bits.append("20日量能净变化为正，累计放量" if vsumd > 0 else "20日量能净变化为负，累计缩量")
    return Dimension("volume", "量能结构", round(score, 1), "；".join(bits), evidence)


def _pricevolume(v: dict[str, Value]) -> Dimension:
    """量价配合：CORR 为价格与对数成交量的相关性。"""
    parts: list[float | None] = []
    evidence: list[str] = []

    corr: dict[int, float] = {}
    for window in (10, 20, 60):
        value = v.get(f"CORR{window}")
        if value is None:
            continue
        corr[window] = value
        parts.append(_scale(value, 0.0, 0.6))
        evidence.append(f"{window}日量价相关性 CORR{window}={value:+.3f}")

    cord20 = v.get("CORD20")
    if cord20 is not None:
        parts.append(_scale(cord20, 0.0, 0.6))
        evidence.append(f"涨跌—量变相关性 CORD20={cord20:+.3f}")

    score = _avg(parts)
    if score is None:
        return Dimension("pricevolume", "量价配合", None, "量价相关性因子数据不足", evidence)

    # 结论必须与得分同源：得分取的是多窗口均值，描述也就以均值为准，
    # 否则会出现"得分 74 偏强、结论却说量价关系不明显"的自相矛盾。
    if not corr:
        detail = "量价相关性数据有限"
    else:
        mean_corr = sum(corr.values()) / len(corr)
        spread = "/".join(f"{w}日{v:+.2f}" for w, v in sorted(corr.items()))
        if mean_corr >= 0.4:
            head = f"多窗口量价正相关（均值 {mean_corr:+.2f}），上涨伴随放量，属于较健康的量价配合"
        elif mean_corr <= -0.4:
            head = f"多窗口量价负相关（均值 {mean_corr:+.2f}），价格与成交量背离，需警惕量价不匹配"
        elif mean_corr >= 0.15:
            head = f"量价弱正相关（均值 {mean_corr:+.2f}），放量与上涨大体同步但不牢固"
        elif mean_corr <= -0.15:
            head = f"量价弱负相关（均值 {mean_corr:+.2f}），量能与价格方向略有背离"
        else:
            head = f"量价相关性接近零（均值 {mean_corr:+.2f}），量价关系不明显"
        detail = f"{head}；分窗口 {spread}"
    return Dimension("pricevolume", "量价配合", round(score, 1), detail, evidence)


def _strength(v: dict[str, Value]) -> Dimension:
    """涨跌强弱：类 RSI 的动能占比与上涨天数占比。"""
    parts: list[float | None] = []
    evidence: list[str] = []

    sumd20 = v.get("SUMD20")
    if sumd20 is not None:
        parts.append(_scale(sumd20, 0.0, 0.6))
        evidence.append(f"20日净动能 SUMD20={sumd20:+.3f}")
    sumd60 = v.get("SUMD60")
    if sumd60 is not None:
        evidence.append(f"60日净动能 SUMD60={sumd60:+.3f}")
    sump20 = v.get("SUMP20")
    if sump20 is not None:
        parts.append(_clip(sump20 * 100.0))
        evidence.append(f"20日上涨动能占比 {sump20 * 100:.1f}%（类 RSI）")
    cntd20 = v.get("CNTD20")
    if cntd20 is not None:
        parts.append(_scale(cntd20, 0.0, 0.4))
        evidence.append(f"20日涨跌天数差 CNTD20={cntd20:+.2f}")
    cntp20 = v.get("CNTP20")
    if cntp20 is not None:
        evidence.append(f"20日上涨天数占比 {cntp20 * 100:.0f}%")

    kmid = v.get("KMID")
    if kmid is not None:
        evidence.append(f"最新交易日实体涨跌 {kmid * 100:+.2f}%（KMID）")

    score = _avg(parts)
    if score is None:
        return Dimension("strength", "涨跌强弱", None, "强弱类因子数据不足", evidence)

    bits: list[str] = []
    if sump20 is not None:
        if sump20 >= 0.6:
            bits.append(f"20日上涨动能占比 {sump20 * 100:.1f}%，多头动能占优（类 RSI 偏高）")
        elif sump20 <= 0.4:
            bits.append(f"20日上涨动能占比 {sump20 * 100:.1f}%，空头动能占优（类 RSI 偏低）")
        else:
            bits.append(f"20日上涨动能占比 {sump20 * 100:.1f}%，多空动能基本均衡")
    if cntp20 is not None:
        bits.append(f"20日内 {cntp20 * 100:.0f}% 的交易日收阳")
    return Dimension("strength", "涨跌强弱", round(score, 1), "；".join(bits), evidence)


# ---------------------------------------------------------------------------
def cross_section(profiles: list[FactorProfile]) -> list[tuple[str, float]]:
    """横截面排序：按综合分给标的排队（仅用于呈现分布，不构成推荐）。"""
    rows = [(p.name, p.composite) for p in profiles if p.composite is not None]
    return sorted(rows, key=lambda r: -r[1])  # type: ignore[arg-type,return-value]


def dimension_table(profile: FactorProfile) -> list[tuple[str, str, str]]:
    """给渲染层用的三列表：维度 / 得分 / 判读。"""
    rows: list[tuple[str, str, str]] = []
    for dim in profile.dimensions:
        score = "—" if dim.score is None else f"{dim.score:.0f}"
        rows.append((dim.label, score, dim.level))
    return rows
