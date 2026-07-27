# 部署：启用 GitHub Actions 定时抓取

`scrape.yml` / `test.yml` 放在这里而不是 `.github/workflows/`，是因为
本次提交所用的 GitHub App 没有 `workflows` 权限，无法直接推送 workflow 文件。

**你只需要执行一次下面的命令，定时任务就会生效。**

## 一、启用 workflow

```bash
git checkout arena/019fa148-272
mkdir -p .github/workflows
cp deploy/github-workflows/*.yml .github/workflows/
git add .github/workflows
git commit -m "启用 GitHub Actions 定时抓取"
git push
```

也可以直接在 GitHub 网页上操作：进入仓库 → **Add file → Create new file**，
路径填 `.github/workflows/scrape.yml`，把 `deploy/github-workflows/scrape.yml`
的内容粘进去保存即可。

## 二、配置 Secret

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Name | Value | 必填 |
|---|---|---|
| `PUSHPLUS_TOKEN` | `26614f5b8a874aab9ad4791555079520` | ✅ |
| `PUSHPLUS_TOPIC` | 群组编码，只发给自己就不用建 | ❌ |
| `DATAYES_TOKEN` | 萝卜投研 Cloud-Sso-Token | ❌ |

> 没配 `DATAYES_TOKEN` 时萝卜投研会自动降级为东财"机构盈利预测"，
> 推送页脚会如实标注，不影响其余九个源。

## 三、验证

1. 打开仓库 **Actions** 标签页，如提示则点击启用 workflow。
2. 选择 **章鱼AI 抓取推送 → Run workflow**，`dry_run` 填 `true` 先跑一次。
3. 跑完在 Artifacts 里下载 `preview.html` 看效果；确认无误后改回 `false`，
   之后每 30 分钟（`cron: "0,30 * * * *"`）自动执行。

## 关于定时精度

GitHub Actions 的 `schedule` 在整点高峰期常有 5-15 分钟延迟，偶尔会跳过。
本项目的设计已经考虑了这一点：

- 时间窗口 180 分钟 ≫ 抓取间隔 30 分钟，延迟不会漏内容；
- `state/seen.json` 做跨轮次去重，重叠也不会重复推送。

如果你需要**准点**执行，用服务器常驻模式更稳：

```bash
export PUSHPLUS_TOKEN=26614f5b8a874aab9ad4791555079520
nohup python main.py --loop > octopus.log 2>&1 &
```
