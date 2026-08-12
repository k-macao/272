# 章鱼 AI · A股情报抓取智能体

两种用法，都推送到微信 PushPlus：

1. **定时情报抓取**——每 30 分钟扫描十个金融数据源，**严格校验每条内容的发布时间**，整合后推送。
2. **主题因子分析（一对一）**——[只输入一个主题](#主题因子分析一对一只输入主题)，
   自动站在 A 股市场监督管理视角，调用 GitHub 上的开源量化因子模型（microsoft/qlib Alpha158）
   分析市场，生成研究报告后一对一推送：

   ```bash
   python main.py --theme "人形机器人"
   ```

推送样式：浅灰底色 `#eceff3` + 深蓝字体 `#12305c`。

---

## 快速开始

```bash
pip install -r requirements.txt

export PUSHPLUS_TOKEN=你的token
# 一对多群组推送（可选，逗号分隔多个群组编码）
export PUSHPLUS_TOPICS=group1,group2
python main.py --dry-run    # 先本地预览，正文写入 preview.html
python main.py              # 抓取并推送一次
python main.py --loop       # 常驻，每 30 分钟一轮
```

GitHub Actions 部署（推荐，免服务器）：

```bash
mkdir -p .github/workflows && cp deploy/github-workflows/*.yml .github/workflows/
git add .github/workflows && git commit -m "启用定时抓取" && git push
```

然后在 **Settings → Secrets and variables → Actions** 新建 `PUSHPLUS_TOKEN`。
详细步骤见 [`deploy/README.md`](deploy/README.md)。

> workflow 文件暂放在 `deploy/github-workflows/`，是因为本次提交所用的
> GitHub App 没有 `workflows` 权限，需要你手动 `cp` 一次来激活。

> ⚠️ 未配置 `PUSHPLUS_TOKEN` 时定时任务会失败退出——这是刻意设计，让你第一时间发现配置缺失，而不是静默空跑。

---

## 十个数据源

| 源 | 抓取内容 | 时间戳来源 | 精度 |
|---|---|---|---|
| **问财·同花顺** | 涨停池（封板原因/封单/连板数）、个股人气榜 | `last_limit_up_time` Unix 秒 | 精确到秒 |
| **东方财富** | 7×24 快讯，重点识别题材异动播报 | `showtime` | 精确到秒 |
| **证券之星** | 盘中异动快报（涨跌停触发） | 列表页时间前缀 | 精确到秒 |
| **巨潮资讯** | 上市公司公告（业绩预告/回购/减持/问询函） | `announcementTime` | 精确到秒 |
| **iFinD·资金情绪** | 行业板块涨跌+主力净流入、北向资金 | 行情快照分钟刻度 | 推算（30分钟对齐） |
| **集思录** | 转债强赎提醒、盘中异动、双低榜 | `last_time` 成交时间 | 精确到秒 |
| **迈博汇金** | 券商研报聚合（机构/评级/行业） | `publishDate` | 精确到天 |
| **慧博投研** | 宏观政策、行业景气度研报 | 列表页日期列 | 精确到天 |
| **国家统计局** | PMI/CPI/GDP/社融等官方数据发布 | RSS `pubDate` | 精确到秒 |
| **萝卜投研** | 机构盈利预测与评级变动 | `publishDate` | 精确到天 |

---

## 时间校验机制（核心）

需求里"要检验时间"是这个项目最关键的约束。实现上分四层：

### 1. 时间质量分级

每条内容的时间戳都被标注可信度，推送里会显示对应徽标：

| 等级 | 含义 | 处理 |
|---|---|---|
| `EXACT` | 源给出精确到分/秒的发布时间 | 直接采用 |
| `DATE` | 源只给到日期 | 仅研报/宏观类源放行，对齐到当天 08:00/09:00 |
| `DERIVED` | 由相对时间（"46分钟前"）或快照刻度推算 | 标注"推算"徽标 |
| `MISSING` | 没有时间信息 | **一律丢弃** |

### 2. 格式归一

各站点的时间格式五花八门，`octopus/timeutil.py` 统一处理，并全部归到东八区：

- 东财公告的 `2026-07-27 07:53:06:817`（毫秒用**冒号**分隔的非标准格式）
- 同花顺的字符串型 Unix 秒 `"1785116229"`
- 集思录的 `10:05:50`（**只有时间没有日期**，需推断是今天还是昨天）
- 研报的 `2026-07-27 00:00:00.000`（零点不是真的零点发布，**降级为 DATE**）
- 相对时间 `46分钟前`、`刚刚`

> **原则：宁可丢弃，不可臆造。** 解析不出来就返回 `None` 让条目被过滤，绝不用"抓取时刻"冒充"发布时刻"。

### 3. 新鲜度与异常检测

- **时间窗口**：默认只推送最近 **180 分钟**内发布的内容（`window_minutes`）
- **未来时间检测**：超过当前时刻 10 分钟的时间戳视为脏数据丢弃（源站时钟漂移在容忍内放行）
- **交易时段感知**：盘中快照类源（板块热力、北向资金、人气榜）仅在开市时段产出，避免夜间刷屏

### 4. 跨轮次去重

30 分钟跑一次、窗口 180 分钟，必然重叠。`state/seen.json` 记录已推送条目的指纹（URL 优先，无 URL 则用 源+标题 哈希），保留 72 小时后自动清理。

**推送失败时不记账**——否则内容会永久丢失，下一轮还能重推。

---

## 可靠性设计

- **单源隔离**：任何一个源抛异常都被 `Source.run()` 捕获，只影响该源，其余九个正常推送，失败源在页脚如实列出
- **自动降级**：巨潮直连失败 → 东财公告镜像；迈博汇金不可达 → 东财研报中心；萝卜投研未配 token → 东财机构预期。**每次降级都会在推送里标注**，不假装数据来自原站
- **并发抓取**：十个源并行，单轮通常 5-10 秒完成
- **重试退避**：网络层自动重试 2 次，指数退避 + 抖动

---

## 夜间免打扰（23:00 后暂停推送，07:00 起床）

北京时间 **23:00 至次日 07:00** 为免打扰时段（`quiet_start` – `quiet_end`）：

- **定时轮次整体跳过**：不抓取、不推送、不动去重账本，日志只留一行
  "本轮暂停抓取与推送"。GitHub Actions 的 cron 照常触发，进入后秒级退出（成功态）。
- **`--loop` 常驻模式直接睡到起床点**：23:00 后不再每 30 分钟空转，一觉到 07:00 再起轮。
- **起床后不补推隔夜内容**：时间窗口 180 分钟，隔夜条目本就已过期——早晨第一轮只推当下新鲜的。
- **人工操作不受限**：`--dry-run` 本地预览照常生成；手动推送（`--manual` / `--manual-web` / 手动 workflow）随时可用。

关闭或调整（`config.yml`，环境变量 `OCTOPUS_QUIET_START` / `OCTOPUS_QUIET_END` 优先）：

```yaml
quiet_start: "23:00"   # 留空即关闭；两端相同也视为关闭
quiet_end: "07:00"     # 建议加引号，防止 YAML 把未加引号的 23:00 解析成六十进制数
```

---

## 配置

`config.yml` 可调项（环境变量优先级更高）：

```yaml
window_minutes: 180        # 时间窗口
interval_minutes: 30       # 循环间隔
quiet_start: "23:00"       # 夜间免打扰开始（北京时间），留空关闭
quiet_end: "07:00"         # 夜间免打扰结束（起床点）
max_items_per_source: 0    # 单源条数上限；0 = 不限，抓取内容全量进推送
max_items_total: 0         # 单条推送总条数上限；0 = 不限（一条推送含全部抓取内容）
push_when_empty: true      # 无新增时仍推一条"本轮无新增"播报
disabled_sources: []       # 临时关掉某些源，如 [datayes, hibor]
```

> 默认配置下**一条推送包含全部抓取内容**——没有单源截断、没有总条数上限，
> 唯一的筛选仍是时间校验（无时间戳/未来时间/超出窗口的一律丢弃）与跨轮去重。
> 哪天觉得刷屏了，把两个上限改回正整数（如 8 / 60）即可恢复截断。

常用命令行参数：

```bash
python main.py --window 60              # 临时改时间窗口
python main.py --sources iwencai,cninfo # 只跑指定源
python main.py --verbose                # 调试日志
python main.py --theme "人形机器人"      # 主题因子分析（见下一节）
```

---

## 主题因子分析（一对一，只输入主题）

**输入一个主题，其余全自动**：站在 A 股市场监督管理视角，调用 GitHub 上的开源
量化因子模型分析市场，生成研究报告并一对一推送到微信。

```bash
python main.py --theme "人形机器人"              # 分析并推送
python main.py --theme "创业板" --dry-run        # 只出预览，写入 preview.html
python main.py --theme "半导体" --stock-top 8    # 多看几只成分股
python main.py --theme "储能" --no-ai            # 不调大模型，用内置规则化解读
python main.py --theme "人形机器人" --market-source yahoo   # 指定用国外免费源
```

### 它做了什么

```
主题
 ├─→ ① 板块匹配      概念/行业板块（东财实时板块 或 内置概念词典）
 │                     → 命中板块内成交额前 N 只个股 + 基准指数
 ├─→ ② 因子模型      GitHub 实时拉取 microsoft/qlib 的 Alpha158 因子定义
 ├─→ ③ 因子计算      在真实日线上求值 → 压缩成六维画像
 ├─→ ④ 市场监督管理   抓取问询函/立案/异常波动等监管事件，评估监管风险等级
 ├─→ ⑤ AI 解读       把「算好的事实清单」交给 DeepSeek 写报告
 └─→ ⑥ 合规审查      违规荐股表述检测与中性化改写 → PushPlus 一对一推送
```

### ① 行情数据源：国内东财 / 国外 Yahoo 免费源（可切换、自动降级）

主题分析需要 A 股行情数据。**国内机器**直连东方财富接口（板块/主力资金/换手率
最全）；**国外机器**（海外 VPS、GitHub Actions 的服务器在美国）也能跑：
新增了 **Yahoo Finance** 作为免费数据源 —— 免注册、无 API Key，直接读
`600519.SS / 000001.SZ` 这类代码的日线与实时报价。

```bash
python main.py --theme "人形机器人" --market-source yahoo   # 只用 Yahoo
python main.py --theme "人形机器人" --market-source auto    # 默认：东财优先，失败自动降级
```

| 配置值 | 行为 |
|---|---|
| `auto`（默认） | 先试东财；任一环节连不上自动降级 Yahoo |
| `eastmoney` | 只用东方财富接口（国内机器） |
| `yahoo` | 只用 Yahoo Finance（国外机器，免费免 Key） |

环境变量 `OCTOPUS_FACTOR_MARKET_SOURCE` 或 `config.yml` 的
`factor_market_source` 同样生效。报告里会如实标注本次实际用了哪个数据源。

**Yahoo 源的差异（全部如实标注，不假装数据来自东财）：**

- 板块列表：Yahoo 不提供 A 股概念板块，改用**内置概念词典**
  （`octopus/factor/concepts.py`，约 80 个概念/行业 × 代表性个股），
  主题匹配逻辑不变；
- 板块涨跌幅：由成分股最新行情**推算**，报告标注「（推算）」；
- 个股成交额：由「最新价 × 成交量」**推算**，用于板块内排序；
- 换手率 / 主力净流入：Yahoo 不提供，报告显示「数据缺失」；
- 日线为**前复权**（原始价 × 复权因子，与 yfinance 同口径），
  日期由接口给出并校验，解析不出的整根丢弃。

### ① 调用 GitHub 上的因子模型（不是本地写死的公式）

因子定义**实时从 GitHub 拉取**，报告里带 commit sha 可溯源：

> 因子模型：`microsoft/qlib@a7d5a9b，2024-07-05 · Alpha158（GitHub API 实时拉取）`

具体做法是抓取 [`qlib/contrib/data/loader.py`](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)，
用 AST 解析出 `Alpha158DL.get_feature_config` 里的 158 个因子表达式。

**为什么不 `pip install qlib`**：qlib 依赖 numpy/pandas/cython/redis，CI 里装一次两三分钟；
而我们只需要「因子公式」这份知识，公式就写在 loader.py 里。解析出来的是纯字符串表达式，
由自研的白名单引擎求值，**不执行仓库里的任何代码**。

取数分四级降级，每一级都会如实标注在推送里：

| 顺序 | 来源 | 报告中的标注 |
|---|---|---|
| 1 | GitHub API（带 commit sha 与提交时间） | `GitHub API 实时拉取` |
| 2 | raw.githubusercontent 直连 | `raw 直连（无版本号）` |
| 3 | 本地缓存 `state/factors/` | `GitHub 不可达，使用本地缓存` |
| 4 | 内置 Alpha158 快照 | `GitHub 不可达，使用内置快照` |

> 内置快照与 GitHub 上的 158 个因子**逐字节一致**（有测试保证），
> 断网也能出报告，但绝不假装数据来自 GitHub。

### ② 因子表达式引擎（`octopus/factor/expr.py`）

qlib 的因子长这样，需要一个求值器：

```python
"Std(Abs($close/Ref($close, 1)-1)*$volume, 30)/(Mean(Abs($close/Ref($close, 1)-1)*$volume, 30)+1e-12)"
```

纯标准库实现了 28 个算子（`Ref/Mean/Std/Corr/Slope/Rsquare/Resi/Quantile/IdxMax/WMA/EMA…`），
全部与 pandas/numpy 逐一对拍验证过数值一致性（`ddof=1`、分位数线性插值、`rank(pct=True)` 等语义细节都对齐）。

**安全性**：表达式虽来自公开仓库，仍按不可信输入处理 —— 先 AST 解析做节点白名单校验，
只放行算术/比较/白名单函数调用，禁止属性访问、下标、lambda、推导式，
`eval` 时 `__builtins__` 置空。`__import__('os').system(...)`、`$close.__class__` 之类一律拒绝。

**缺失即缺失**：窗口不足、除零、常数序列求相关、溢出，一律返回 `None`，
报告里如实写「数据不足」——沿用项目「宁可丢弃，不可臆造」的一贯原则。

### ③ 六维因子画像

158 个原始因子值（`MA20=0.9539`）直接甩给读者毫无意义，聚合成六个维度（0-100 分）：

| 维度 | 用到的因子 | 解读的是什么 |
|---|---|---|
| 价格动量 | ROC5/10/20/60 | 多周期涨跌幅，长短周期是否同向 |
| 趋势结构 | MA5-60、BETA、RSQR、RESI | 价格相对均线位置、趋势斜率与线性度 |
| 波动与位置 | STD、RANK、RSV、MAX/MIN | 波动率高低、价格在区间中的位置 |
| 量能结构 | VMA、VSTD、VSUMD、WVMA | 放量还是缩量、量能是否稳定 |
| 量价配合 | CORR10/20/60、CORD | 上涨是否伴随放量，有无量价背离 |
| 涨跌强弱 | SUMP/SUMN/SUMD、CNTP/CNTD | 类 RSI 的多空动能占比 |

每个维度都给出**引用具体读数的中性判读**，而不是空泛的「偏强」：

> 量价配合[80/偏强]：多窗口量价正相关（均值 +0.53），上涨伴随放量，属于较健康的量价配合；分窗口 10日+0.64/20日+0.54/60日+0.42

### ④ A 股市场监督管理视角

这一层是双向的：

**监管动态**——抓取窗口内（默认 30 天）的监管类公告，按严重度分级：

| 类别 | 严重度 | 例子 |
|---|---|---|
| 立案调查 | 95-100 | 收到中国证监会立案告知书 |
| 行政处罚 / 市场禁入 | 88-92 | 行政处罚决定书 |
| 退市风险 | 80-90 | 其他风险警示、终止上市 |
| 监管措施 / 纪律处分 | 72-80 | 警示函、责令改正、公开谴责 |
| 问询关注 | 66-70 | 问询函、关注函、监管函 |
| 异常波动 | 58-65 | 股票交易异常波动公告 |

风险等级**只看与分析标的直接相关的事件** —— 全市场每天都有问询函，那是背景噪音；
真正要紧的是「我们正在分析的这几只票」有没有被监管点名。所有监管信息同样经过
时间校验：无时间戳、未来时间、超窗口的一律丢弃。

**合规审查**——约束我们自己的输出。大模型可能写出「建议立即买入，目标价 80 元，稳赚不赔」，
这违反《证券法》与《发布证券研究报告暂行规定》。审查层会检出并中性化改写：

```
改写前：板块内部分化明显，建议立即买入汇川技术，目标价：80元，稳赚不赔。
改写后：板块内部分化明显，从研究角度观察到汇川技术，（不提供目标价位），（不涉及收益预测）。
```

覆盖四类红线：买卖指令与目标价、收益承诺与绝对化断言、内幕信息暗示、操纵/跟庄引导。
改写是**幂等**的（替换文本自身不会二次触发规则，有测试保证），
推送末尾强制附加风险提示与免责声明，报告里如实展示「本次触发了几处合规改写」。

### ⑤ AI 只解读，不编造

大模型拿到的是**已经算完的事实清单**（因子读数、监管事件、数据口径），
系统提示词明确要求「只能使用清单中提供的数字，严禁编造任何数据、股票代码、机构观点」，
并内置合规约束。模型只负责组织语言与归因，接触不到原始数据，也就无从编造。

未配置 `DEEPSEEK_API_KEY` 时自动降级为**内置规则化解读**，报告结构与措辞口径完全一致，
只是标注为「🧮 规则化因子解读」而非「✨ DeepSeek AI 解读」——不假装用了 AI。

### GitHub Actions 手动触发

`deploy/github-workflows/theme_analysis.yml` 是独立的 workflow：
**Actions → 章鱼AI 主题因子分析 → Run workflow**，填入主题即可一对一推送
（可选个股数量、监管回溯天数、是否用 AI、是否只出预览）。

> 首次使用需把该文件复制到 `.github/workflows/`（推送用的 GitHub App
> 没有 `workflows` 权限）。同目录下的 `theme_analysis.yml.txt` 是粘贴用过渡文件，
> 可直接在 GitHub 网页上新建文件后粘贴，详见 [`deploy/README.md`](deploy/README.md)。

### 配置

```yaml
factor_stock_top: 6        # 取板块内成交额前几只个股
factor_kline_limit: 250    # 取多少根日线算因子（60日窗口至少要 70 根）
supervision_days: 30       # 监管动态回溯天数
github_token: ""           # 选填，仅用于提高 GitHub API 限额
factor_market_source: auto # 行情数据源：auto（东财优先自动降级）/ eastmoney / yahoo
```

---

## 手动主题分析推送（一对一）

除了定时抓取，也可以**人工录入一段 AI 分析内容**（主题 + 正文），
用同一套浅灰底深蓝字样式渲染成独立推送发到微信，不经过抓取/时间校验/去重。

> **DeepSeek 大模型智能提炼**：如在环境变量或 `config.yml` 中配置了 `DEEPSEEK_API_KEY`（DeepSeek 大模型 API Key），
> 无论是网页录入、命令行还是 GitHub Actions 手动执行，系统都会**自动调用 DeepSeek API (`deepseek-v4-flash`)**
> 对您输入的主题和内容进行深度提炼、分类和摘要，最终组合成为包含「✨ DeepSeek AI 智能提炼」与「📝 原始录入内容」
> 两个卡片于一体的精美微信推文！

> **一对一**：手动推送恒为 PushPlus **个人推送**——只发给 token 所属账号本人，
> **不携带群组 topic**，与 `config.yml` 里的 `pushplus_topics: oai.1`（一对多）互不影响。

### 方式一：独立网页页面（推荐）

本地起一个独立的推送页面，浏览器里输入主题/内容、预览、一键发送：

```bash
python main.py --manual-web              # 打开 http://127.0.0.1:8765
python main.py --manual-web --port 9000  # 换端口
python main.py --manual-web --host 0.0.0.0   # 服务器/局域网使用
```

页面与抓取流水线完全独立，自带「预览效果」（和微信收到的渲染一致）与「发送推送」。
绑定 `0.0.0.0` 时建议设置访问令牌：

```bash
export OCTOPUS_WEB_TOKEN=你的令牌   # 页面需输入令牌才能推送
```

### 方式二：命令行

```bash
# 参数直给
python main.py --manual --topic "机器人板块分析" \
  --content "今日机器人板块放量上涨，情绪回暖，关注减速器方向……"

# 从文件/管道读入内容（内容可以很长）
python main.py --manual --topic "机器人板块分析" < analysis.md

# 终端交互输入（先输主题，再逐行输内容，完成后按 Ctrl+D）
python main.py --manual

# 只生成预览不推送（写入 preview.html）
python main.py --manual --topic "机器人板块分析" --content "……" --dry-run
```

- `--topic` 是可选的；不给就只推内容，标题显示为「主题」。
- 手动模式忽略 `--loop` / `--sources` / `--window`，推一次即退出。
- 推送失败返回退出码 1，方便在 CI 里显式发现。

### 方式三：独立的启动 yml（GitHub Actions 手动触发）

`deploy/github-workflows/pushplus_workflow.yml.txt` 是可直接复制的过渡文件。
由于 GitHub App 没有 Workflows 写入权限，请打开该文件并复制全部内容，在 GitHub
网页中新建 `.github/workflows/pushplus_workflow.yml` 后粘贴提交。确认新入口可用后，
删除旧的 `.github/workflows/m.yml`，避免 Actions 出现两个重复入口。

启用后，仓库 **Actions → 章鱼AI 手动主题推送 → Run workflow**，在表单里填主题与
多行内容即可一对一推送（`dry_run: true` 可先出预览再下载）。该 workflow 只传
`PUSHPLUS_TOKEN`，不传群组 topic，保证一对一。

---

## 项目结构

```
octopus/
├── timeutil.py       # 时间解析与校验 —— 核心防线
├── models.py         # Item / SourceResult / TimeQuality
├── state.py          # 跨轮次去重（原子写入）
├── http.py           # 重试、超时、UA 伪装
├── ai.py             # DeepSeek 大模型：内容提炼 + 主题因子报告解读
├── render.py         # 浅灰底 + 深蓝字的 HTML 渲染
├── notify.py         # PushPlus 推送
├── agent.py          # 主流程编排
├── webui.py          # 独立的手动推送网页（一对一）
├── sources/          # 十个情报源，各自独立
└── factor/           # 主题因子分析（一对一，只输入主题）
    ├── qlib_repo.py    # 从 GitHub 拉 microsoft/qlib 的 Alpha158 因子定义
    ├── expr.py         # qlib 因子表达式引擎（28 个算子，AST 白名单沙箱）
    ├── market.py       # 主题 → 板块 → 成分股 → 日线序列
    ├── scoring.py      # 158 个因子 → 六维画像
    ├── supervision.py  # A股市场监督管理：监管事件抓取与风险分级
    ├── compliance.py   # 输出端合规审查：违规荐股表述检测与中性化改写
    └── pipeline.py     # 全流程编排（含无大模型时的规则化降级解读）
tests/                # 312 个测试，全离线
└── fixtures/         # 真实接口响应样本
```

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

312 个测试全部离线运行（用真实抓取的响应样本做 fixture），不依赖网络。

**情报抓取**（143 个）：时间解析的各种畸形格式、过滤逻辑、去重、降级路径、HTML 转义、
条数上限语义（0 = 全量）、手动主题分析推送（一对一语义 / 独立网页 / 令牌保护 / DeepSeek 提炼）与端到端流程。

**主题因子分析**（169 个）：

- `test_factor_expr.py` —— 28 个算子的语义正确性（与 pandas/numpy 对拍过）、
  缺失值传染、真实 Alpha158 表达式求值，以及 AST 沙箱对注入攻击的拒绝
- `test_factor_repo.py` —— GitHub 源码解析（含内部函数、列表推导等语法特征）、
  四级降级链、缓存读写、篡改源码的安全拒绝、内置快照 158 个因子全部可求值
- `test_factor_supervision.py` —— 监管事件分级、时间校验（无时间/未来/超窗口一律丢弃）、
  风险等级只看标的相关事件、合规审查的检出与幂等改写
- `test_factor_pipeline.py` —— 板块匹配、K线解析、ST 股过滤、六维评分的方向正确性
  （措辞必须与得分同向）、全链路编排、各环节失败时的降级、渲染的微信兼容性与
  XSS 转义、`push_theme` 的一对一语义

---

内容由程序自动抓取整合，仅供参考，不构成投资建议。
