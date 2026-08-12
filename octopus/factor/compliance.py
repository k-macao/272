"""输出端合规审查 —— A 股市场监督管理要求在「我们自己的表述」上落地。

监管对荐股类内容有明确红线（《证券法》第一百六十一条、
《发布证券研究报告暂行规定》、《证券投资顾问业务暂行规定》等）：
未取得投顾资质不得提供具体投资建议，任何人不得对收益作出承诺。

这个模块做两件事：
  1. **扫描**：检出违规表述（荐股指令、收益承诺、绝对化断言、内幕信息暗示）
  2. **中性化**：把违规表述改写成研究性、中性的说法，并强制附加风险提示

设计取向：宁可措辞保守，也不让一句"必涨/建议买入"流出去。
所有改写都会被记录，报告里如实展示"本次触发了几处合规改写"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: (正则, 类别, 替换文本或 None 表示仅告警)
RULES: tuple[tuple[str, str, str | None], ...] = (
    # --- 明确的买卖指令 ---------------------------------------------
    (r"建议(立即|马上|尽快)?(买入|卖出|加仓|减仓|清仓|抄底|逃顶)", "投资建议", "从研究角度观察到"),
    (r"(强烈|重点)?推荐(买入|持有|关注该股|该股)", "投资建议", "研究上值得跟踪"),
    (r"(可以|可)(大胆|放心)?(买入|重仓|满仓|梭哈|上车)", "投资建议", "相关变化可作为研究观察点"),
    (r"(目标价|止损价|买入价|建仓价)\s*[:：]?\s*[\d.]+\s*元?", "价格指令", "（不提供目标价位）"),
    (r"(现价|回调至)?[\d.]+元?(以下|附近)可(买|入|建仓)", "价格指令", "（不提供买卖价位）"),
    # --- 收益承诺 / 绝对化断言 --------------------------------------
    (r"(明天|下周|下月|后市)(必|一定|肯定)(涨|跌|拉升|大涨)", "收益承诺", "后续走势存在不确定性"),
    (r"(必|一定|肯定|铁定|100%|百分百)(涨|跌|赚|盈利|翻倍)", "收益承诺", "存在向上/向下的可能性"),
    (r"(稳赚|包赚|稳赢)(不赔)?", "收益承诺", "（不涉及收益预测）"),
    (r"(保本|保收益|无风险|零风险)(套利)?", "收益承诺", "（不涉及收益承诺）"),
    (r"(涨停|翻倍|暴涨|暴富|十倍股|牛股)(在望|可期|无疑|确定)", "收益承诺", "价格走势存在不确定性"),
    (r"(闭眼|无脑)(买|入|抄)", "投资建议", "需自行独立判断"),
    # --- 内幕信息 / 操纵暗示 ----------------------------------------
    (r"(内幕消息|小道消息|内部消息|独家消息|未公开信息)(显示|表明)?", "内幕信息", "公开信息显示"),
    (r"(主力|游资|机构)(已经)?(建仓|锁仓|控盘)完毕", "操纵暗示", "资金面数据出现变化"),
    (r"(跟(着|随)?(主力|游资|庄家))", "操纵暗示", "结合资金面数据观察"),
    (r"(拉升|出货|洗盘)(计划|时间表)", "操纵暗示", "价量变化"),
)

#: 必须出现在推送末尾的风险提示（监管合规底线）
DISCLAIMER_LINES: tuple[str, ...] = (
    "本报告由程序自动生成，基于公开市场数据与开源量化因子模型计算，"
    "仅用于研究与信息参考，不构成任何证券的投资建议或买卖要约。",
    "量化因子为历史统计规律的刻画，不代表未来收益；历史表现不预示未来表现，"
    "市场有风险，投资需谨慎。",
    "本内容不提供具体标的的买卖指令、目标价位或收益承诺。"
    "投资者应独立判断并自行承担投资风险。",
)


@dataclass
class ComplianceHit:
    """一处合规问题。"""

    category: str
    matched: str
    replaced: str = ""

    @property
    def fixed(self) -> bool:
        return bool(self.replaced)


@dataclass
class ComplianceResult:
    """一次合规审查的结果。"""

    text: str                                   # 审查（并可能改写）后的文本
    hits: list[ComplianceHit] = field(default_factory=list)
    original_length: int = 0

    @property
    def clean(self) -> bool:
        return not self.hits

    @property
    def fixed_count(self) -> int:
        return sum(1 for h in self.hits if h.fixed)

    def summary(self) -> str:
        if not self.hits:
            return "合规审查通过：未检出违规荐股或收益承诺表述"
        cats = sorted({h.category for h in self.hits})
        return (
            f"合规审查：检出 {len(self.hits)} 处敏感表述（{'、'.join(cats)}），"
            f"已中性化改写 {self.fixed_count} 处"
        )


def scan(text: str) -> list[ComplianceHit]:
    """只扫描不改写，返回命中列表。"""
    hits: list[ComplianceHit] = []
    for pattern, category, replacement in RULES:
        for match in re.finditer(pattern, text or ""):
            hits.append(
                ComplianceHit(
                    category=category,
                    matched=match.group(0),
                    replaced=replacement or "",
                )
            )
    return hits


def review(text: str) -> ComplianceResult:
    """扫描并中性化改写。"""
    original = text or ""
    result = ComplianceResult(text=original, original_length=len(original))
    working = original
    for pattern, category, replacement in RULES:
        if replacement is None:
            for match in re.finditer(pattern, working):
                result.hits.append(ComplianceHit(category=category, matched=match.group(0)))
            continue
        matches = list(re.finditer(pattern, working))
        if not matches:
            continue
        for match in matches:
            result.hits.append(
                ComplianceHit(category=category, matched=match.group(0), replaced=replacement)
            )
        working = re.sub(pattern, replacement, working)
    result.text = working
    return result


def disclaimer() -> list[str]:
    """标准免责与风险提示文本。"""
    return list(DISCLAIMER_LINES)


#: 交给大模型的系统提示词里必须包含的合规约束
AI_COMPLIANCE_RULES = """严格遵守中国证券市场监管要求：
1. 不得给出任何具体的买入/卖出/加仓/减仓建议，不得提供目标价位或止损价位；
2. 不得作出收益承诺或使用"必涨""稳赚""保本""无风险"等绝对化表述；
3. 不得暗示掌握内幕信息或未公开信息，不得引导跟随主力/游资操作；
4. 只做客观的因子数据解读与风险揭示，措辞保持中性、研究性；
5. 必须客观揭示相关监管风险（如问询函、立案调查、异常波动等）。"""
