"""合并功能测试：多份 Markdown 合并去重与推送渲染"""

import unittest
from pathlib import Path
import tempfile
import os

from octopus.merge import (
    load_markdown_file,
    merge_markdowns,
    dedup_sources,
    MarkdownSource,
)


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


if __name__ == "__main__":
    unittest.main()
