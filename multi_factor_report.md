# 多因子量化模型深度分析报告（框架版）
【数据源】GitHub 热门项目：blowfish_quant (Project 4 Multi-factor)、ml-quant-trading (213 因子 + Alpha101 + ML + Markowitz)；
【因子维度】技术因子 / 基本面因子 / 情绪因子 / 流动性因子 / 风险因子；
【模型层】Factor Engine → Neutralization → Bias Correction → ML (MLP/Transformer) → Markowitz Optimization → Backtest Engine；
【输出】IC / RankIC / Sharpe / MaxDD / 偏度修正报告；
【检查项】因子覆盖度、数据泄露防控、中性化完整性、回测偏差、组合权重约束。
【A股适配】
- 因子：市盈率(PE)、市净率(PB)、动量(12M-1M)、波动率、换手率、北向资金流向、限涨停偏差修正、行业中性化。
- 数据：Wind / Tushare / 东方财富；回测周期建议 2015-2024，剔除ST/暂停。
- 风险：A股涨跌停板导致偏差，需 limit-up/down bias correction；行业集中度高，需行业中性化。
- 模型：Alpha101 公式 + 204 legacy 因子 → Mask-aware Tensor → MLP/Transformer → Cross-sectional Markowitz → Vectorized Backtest。
【检查清单】因子覆盖>20维、IC>0.05、RankIC>0.03、MaxDD<20%、Sharpe>1.0、无数据泄露、无幸存者偏差。
