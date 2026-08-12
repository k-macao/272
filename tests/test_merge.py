"""合并功能测试：多份 Markdown 合并去重与推送渲染"""

import unittest
from pathlib import Path
import tempfile
import os
from datetime import datetime

from octopus.merge import (
    load_markdown_file,
    merge_markdowns,
    dedup_sources,
    MarkdownSource,
)
from octopus.notify import PushPlus, split_html_pages
from octopus.render import render_merge
from octopus.timeutil import CN_TZ


class TestMarkdownLoad(unittest.TestCase):
    def test_extract_title(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.md"
            p.write_text("# 我的标题\n内容", encoding="utf-8")
            src = load_markdown_file(p)
            self.assertEqual(src.title, "我的标题")
            self.assertTrue(src.sha)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_markdown_file("/tmp/不存在的报告.md")


class TestDedup(unittest.TestCase):
    def test_dedup_same_content(self):
        src1 = MarkdownSource(path=Path("a.md"), title="A", content="same", sha="abc", size=4)
        src2 = MarkdownSource(path=Path("b.md"), title="B", content="same", sha="abc", size=4)
        deduped, notes = dedup_sources([src1, src2])
        self.assertEqual(len(deduped), 1)
        self.assertTrue(any("去重" in n for n in notes))

    def test_no_dedup_different(self):
        src1 = MarkdownSource(path=Path("a.md"), title="A", content="foo", sha="111", size=3)
        src2 = MarkdownSource(path=Path("b.md"), title="B", content="bar", sha="222", size=3)
        deduped, notes = dedup_sources([src1, src2])
        self.assertEqual(len(deduped), 2)


class TestMergeMarkdows(unittest.TestCase):
    def test_merge_two_files(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "goldwind.md"
            p2 = Path(td) / "multi.md"
            p1.write_text("# 金风科技\n金风内容", encoding="utf-8")
            p2.write_text("# 多因子报告\n多因子内容", encoding="utf-8")

            merged = merge_markdowns([p1, p2], merge_topic="合并测试")
            self.assertIn("金风科技", merged.content)
            self.assertIn("多因子报告", merged.content)
            self.assertEqual(merged.topic, "合并测试")
            self.assertEqual(len(merged.sources), 2)

    def test_merge_auto_topic(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "a.md"
            p1.write_text("# 报告A\n内容A", encoding="utf-8")
            merged = merge_markdowns([p1])
            self.assertIn("合并", merged.topic)

    def test_merge_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "a.md"
            p1.write_text("# A\n内容", encoding="utf-8")
            merged = merge_markdowns([p1], add_provenance=True)
            self.assertIn("来源追溯", merged.content)
            self.assertIn("指纹", merged.content)

    def test_merge_notes_detection(self):
        """检测多因子+金风同源标注"""
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "goldwind_analysis.md"
            p2 = Path(td) / "multi_factor_report.md"
            p1.write_text("# 金风\n内容1", encoding="utf-8")
            p2.write_text("# 多因子\n内容2", encoding="utf-8")
            merged = merge_markdowns([p1, p2])
            self.assertTrue(any("goldwind" in n or "multi_factor" in n or "已包含" in n for n in merged.notes))


class TestMergeIntegration(unittest.TestCase):
    def test_preview_merge_dry_run(self):
        """Agent preview_merge 不依赖 token"""
        from octopus.config import Config
        from octopus.agent import Agent

        config = Config(pushplus_token="", pushplus_topics=[])
        agent = Agent(config, base_dir=Path.cwd())
        try:
            with tempfile.TemporaryDirectory() as td:
                p1 = Path(td) / "a.md"
                p2 = Path(td) / "b.md"
                p1.write_text("# 报告A\n内容A", encoding="utf-8")
                p2.write_text("# 报告B\n内容B", encoding="utf-8")
                html = agent.preview_merge([str(p1), str(p2)], merge_topic="测试合并")
                self.assertIn("报告A", html)
                self.assertIn("报告B", html)
        finally:
            agent.close()


class TestMergeRendering(unittest.TestCase):
    REF = datetime(2026, 8, 12, 10, 30, tzinfo=CN_TZ)

    def test_markdown_is_reflowed_for_mobile(self):
        content = """# 合并测试

> 合并元数据

## 目录

1. [正文](#正文)

## 正文

- **结论**：内容清楚
- 第二点

| 指标 | 数值 |
|---|---:|
| Rank IC | 0.067 |

```python
print('<safe>')
```
"""
        rendered = render_merge("合并测试", content, ref=self.REF, source_count=2)
        self.assertIn("章鱼 AI · 合并研报", rendered)
        self.assertIn("<table", rendered)
        self.assertIn("<pre", rendered)
        self.assertIn("<strong", rendered)
        self.assertNotIn("## 正文", rendered)
        self.assertNotIn("|---|", rendered)
        self.assertNotIn("[正文](#正文)", rendered)  # 微信内无效的目录已移除
        self.assertIn("&lt;safe&gt;", rendered)

    def test_long_html_splits_only_between_complete_cards(self):
        content = "# 长报告\n\n" + "\n\n".join(
            f"## 章节{i}\n\n" + ("内容" * 4000) for i in range(4)
        )
        rendered = render_merge("长报告", content, ref=self.REF, source_count=1)
        pages = split_html_pages(rendered, max_content=18000)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertTrue(page.startswith("<div"))
            self.assertTrue(page.endswith("</div>"))
            self.assertEqual(page.count("<div"), page.count("</div>"))
            self.assertNotIn("<!--octopus:block-->", page)

    def test_pushplus_adds_page_numbers(self):
        class RecordingHttp:
            def __init__(self):
                self.payloads = []

            def post_json(self, url, payload):
                self.payloads.append(payload)
                return {"code": 200}

        # 使用模块默认 4 万字符阈值，两个独立卡片会被整理成多页。
        blocks = [f"<div>{'甲' * 22000}</div>", f"<div>{'乙' * 22000}</div>"]
        body = "<div>" + "<!--octopus:block-->".join(blocks) + "</div>"
        http = RecordingHttp()
        self.assertTrue(PushPlus(http, "token").send("长报告", body))
        self.assertEqual(len(http.payloads), 2)
        self.assertEqual([p["title"] for p in http.payloads], ["长报告（1/2）", "长报告（2/2）"])
        self.assertTrue(all(p["content"].endswith("</div>") for p in http.payloads))


if __name__ == "__main__":
    unittest.main()
