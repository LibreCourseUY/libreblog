import tempfile
import unittest
from pathlib import Path

import scripts.generate_tldr as g


def make_edition(items: int = 14) -> str:
    first = (items + 1) // 2
    lines = [
        "---",
        "title: v0.9",
        "date: 2026-09-21",
        "---",
        "",
        "Here is what mattered (Sep 14 to 21, 2026).",
        "",
        "### Tech News",
        "",
        "#### Linux",
    ]
    for index in range(first):
        lines.append(f"- **Item {index}** happened ([source](https://example.com/{index})).")
    lines.append("")
    lines.append("#### Infra")
    for index in range(first, items):
        lines.append(f"- **Item {index}** happened ([source](https://example.com/{index})).")
    lines += ["", "---", "", "That's it for this week.", "", "- The Editor"]
    return "\n".join(lines) + "\n"


VALID = make_edition()


class VersionTests(unittest.TestCase):
    def setUp(self):
        self._original = g.TLDR_DIR
        self._tmp = tempfile.TemporaryDirectory()
        g.TLDR_DIR = Path(self._tmp.name)

    def tearDown(self):
        g.TLDR_DIR = self._original
        self._tmp.cleanup()

    def test_next_version_from_empty(self):
        self.assertEqual(g.next_version(), "v0.1")

    def test_next_version_increments_the_minor(self):
        for name in ("v0.3.md", "v0.4.md"):
            (g.TLDR_DIR / name).write_text("---\ntitle: v0.4\n---\n")
        self.assertEqual(g.next_version(), "v0.5")


class SanitizeTests(unittest.TestCase):
    def test_removes_forbidden_dashes(self):
        self.assertEqual(g.sanitize("a \u2014 b \u2013 c -- d"), "a - b - c - d\n")

    def test_strips_a_markdown_code_fence(self):
        self.assertEqual(g.sanitize("```markdown\nhello\n```"), "hello\n")


class ValidateTests(unittest.TestCase):
    def test_accepts_a_valid_edition(self):
        self.assertEqual(g.validate(g.sanitize(VALID), "v0.9"), [])

    def test_counts_news_items(self):
        self.assertEqual(g.news_item_count(VALID), 14)

    def test_rejects_too_few_items(self):
        problems = g.validate(g.sanitize(make_edition(9)), "v0.9")
        self.assertTrue(any("news items" in problem for problem in problems))

    def test_rejects_a_missing_footer(self):
        problems = g.validate(g.sanitize(VALID.replace("- The Editor", "")), "v0.9")
        self.assertTrue(any("footer" in problem for problem in problems))


class FeedTests(unittest.TestCase):
    def test_reads_feeds_from_file(self):
        feeds = g.read_feeds()
        self.assertGreater(len(feeds), 0)
        self.assertTrue(all(feed.startswith("http") for feed in feeds))


if __name__ == "__main__":
    unittest.main()
