# 章鱼 AI · A股情报抓取智能体

每 30 分钟扫描十个金融数据源，**严格校验每条内容的发布时间**，整合后推送到微信 PushPlus。

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

## 配置

`config.yml` 可调项（环境变量优先级更高）：

```yaml
window_minutes: 180        # 时间窗口
interval_minutes: 30       # 循环间隔
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
```

---

## 手动主题分析推送（一对一）

除了定时抓取，也可以**人工录入一段 AI 分析内容**（主题 + 正文），
用同一套浅灰底深蓝字样式渲染成独立推送发到微信，不经过抓取/时间校验/去重。

> **智谱大模型智能提炼**：如在环境变量或 `config.yml` 中配置了 `ZHIPU_API_KEY`（智谱 AI 大模型 API Key），
> 无论是网页录入、命令行还是 GitHub Actions 手动执行，系统都会**自动调用智谱 API (`glm-4-flash`)**
> 对您输入的主题和内容进行深度提炼、分类和摘要，最终组合成为包含「✨ 智谱 AI 智能提炼」与「📝 原始录入内容」
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

`deploy/github-workflows/manual_push.yml` 是**独立于定时抓取**的 workflow：
仓库 **Actions → 章鱼AI 手动主题推送 → Run workflow**，在表单里填主题与多行内容
即可一对一推送（`dry_run: true` 可先出预览再下载）。该 workflow 只传
`PUSHPLUS_TOKEN`，不传群组 topic，保证一对一。与 scrape 一样需要手动
`cp` 到 `.github/workflows/` 后生效（见 `deploy/README.md`）。

---

## 项目结构

```
octopus/
├── timeutil.py       # 时间解析与校验 —— 核心防线
├── models.py         # Item / SourceResult / TimeQuality
├── state.py          # 跨轮次去重（原子写入）
├── http.py           # 重试、超时、UA 伪装
├── ai.py             # 智谱 AI 大模型提炼、分类与摘要（用于手动推送）
├── render.py         # 浅灰底 + 深蓝字的 HTML 渲染
├── notify.py         # PushPlus 推送
├── agent.py          # 主流程编排
├── webui.py          # 独立的手动推送网页（一对一）
└── sources/          # 十个源，各自独立
tests/                # 129 个测试，全离线
└── fixtures/         # 真实接口响应样本
```

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

129 个测试全部离线运行（用真实抓取的响应样本做 fixture），不依赖网络。覆盖时间解析的各种畸形格式、过滤逻辑、去重、降级路径、HTML 转义、条数上限语义（0 = 全量）、手动主题分析推送（一对一语义 / 独立网页 / 令牌保护 / 智谱大模型提炼与摘要）与端到端流程。

---

内容由程序自动抓取整合，仅供参考，不构成投资建议。
