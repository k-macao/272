#!/usr/bin/env python3
"""章鱼 AI · A股情报抓取智能体 —— 入口.

用法：
    python main.py                 # 跑一轮，抓取并推送
    python main.py --dry-run       # 只抓不推，正文存到 preview.html
    python main.py --loop          # 常驻，每 30 分钟一轮
    python main.py --window 60     # 临时改时间窗口（分钟）
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from octopus.agent import Agent
from octopus.config import Config
from octopus.timeutil import now, stamp

BASE_DIR = Path(__file__).resolve().parent

_stop = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    logging.getLogger(__name__).info("收到信号 %s，将在本轮结束后退出", signum)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="octopus",
        description="章鱼 AI · A股十源情报抓取与微信推送",
    )
    p.add_argument("--dry-run", action="store_true", help="不推送，仅把正文写入 preview.html")
    p.add_argument("--loop", action="store_true", help="常驻循环，按 interval 分钟重复执行")
    p.add_argument("--interval", type=int, default=None, help="循环间隔（分钟），默认 30")
    p.add_argument("--window", type=int, default=None, help="时间窗口（分钟），默认 180")
    p.add_argument("--config", default=None, help="配置文件路径，默认 config.yml")
    p.add_argument("--sources", default="", help="只跑指定源，逗号分隔，如 iwencai,cninfo")
    p.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    return p


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("octopus.main")

    config = Config.load(args.config)
    if args.window:
        config.window_minutes = args.window
    if args.interval:
        config.interval_minutes = args.interval
    if args.sources:
        from octopus.sources import REGISTRY

        wanted = {s.strip() for s in args.sources.split(",") if s.strip()}
        unknown = wanted - set(REGISTRY)
        if unknown:
            log.error("未知的源：%s；可选：%s", ", ".join(unknown), ", ".join(REGISTRY))
            return 2
        config.disabled_sources = [n for n in REGISTRY if n not in wanted]

    if not config.pushplus_token and not args.dry_run:
        log.error("缺少 PUSHPLUS_TOKEN（环境变量或 config.yml），"
                  "如只想本地预览请加 --dry-run")
        return 2

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    agent = Agent(config, base_dir=BASE_DIR)
    exit_code = 0
    try:
        while True:
            report = agent.run_once(dry_run=args.dry_run)

            if args.dry_run:
                preview = BASE_DIR / "preview.html"
                preview.write_text(report.html, encoding="utf-8")
                log.info("预览已写入 %s（标题：%s）", preview, report.title)
                _print_summary(report)

            if not report.pushed and not args.dry_run and report.total:
                exit_code = 1  # 有内容却没推出去，让 CI 显式失败

            if not args.loop or _stop:
                break

            sleep_s = max(60, config.interval_minutes * 60)
            log.info("休眠 %d 分钟，下轮约 %s", config.interval_minutes,
                     stamp(now().replace(microsecond=0)))
            slept = 0
            while slept < sleep_s and not _stop:
                time.sleep(min(5, sleep_s - slept))
                slept += 5
            if _stop:
                break
    finally:
        agent.close()

    return exit_code


def _print_summary(report) -> None:
    print("\n" + "=" * 64)
    print(f"扫描时刻 {stamp(report.ref)}   新增 {report.total} 条")
    print("=" * 64)
    for result in sorted(report.results, key=lambda r: r.source):
        flag = "OK " if result.ok else "ERR"
        print(f"  [{flag}] {result.summary_line()}")
    print("-" * 64)
    for result, items in report.groups:
        print(f"\n【{result.source_label}】")
        for item in items:
            when = f"{item.published_at:%m-%d %H:%M}" if item.published_at else "??"
            print(f"  · {when} [{item.time_quality.value:7s}] {item.title[:60]}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
