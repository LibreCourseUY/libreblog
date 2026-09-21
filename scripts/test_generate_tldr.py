import os
import tempfile
import unittest
from datetime import datetime, timezone
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


class FrontMatterTests(unittest.TestCase):
    def test_injects_missing_title_and_date(self):
        result = g.normalize_front_matter("---\ntitle: something\n---\n\nBody\n", "v0.9", "2026-09-21")
        self.assertIn("title: v0.9", result)
        self.assertIn("date: 2026-09-21", result)
        self.assertEqual(result.count("title:"), 1)

    def test_adds_front_matter_when_absent(self):
        result = g.normalize_front_matter("Just a body\n", "v1.0", "2026-09-21")
        self.assertTrue(result.startswith("---\ntitle: v1.0\ndate: 2026-09-21\n---\n"))

    def test_preserves_other_front_matter_fields(self):
        result = g.normalize_front_matter(
            "---\ntitle: x\ndraft: true\n---\n\nBody\n", "v0.9", "2026-09-21"
        )
        self.assertIn("draft: true", result)


class GenerateTests(unittest.TestCase):
    def test_retries_until_valid(self):
        calls = {"count": 0}

        def fake_call(api_key, model, style, prompt):
            calls["count"] += 1
            if calls["count"] == 1:
                return "---\ntitle: v0.9\n---\n\nno sections here\n"
            return make_edition()

        original = g.call_gemini
        g.call_gemini = fake_call
        os.environ["GEMINI_API_KEY"] = "test"
        articles = [
            {
                "source": "s",
                "title": "t",
                "url": "https://example.com/1",
                "published": datetime.now(timezone.utc),
                "summary": "",
            }
        ]
        try:
            result = g.generate(articles, "v0.9", "model", 7)
        finally:
            g.call_gemini = original
            os.environ.pop("GEMINI_API_KEY", None)

        self.assertEqual(calls["count"], 2)
        self.assertEqual(g.validate(result, "v0.9"), [])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self._sleep = g.time.sleep
        g.time.sleep = lambda *_: None

    def tearDown(self):
        g.time.sleep = self._sleep

    def test_retries_transient_failures(self):
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("503 UNAVAILABLE")
            return "ok"

        self.assertEqual(g.retry_call(flaky), "ok")
        self.assertEqual(calls["count"], 3)

    def test_gives_up_after_max_attempts(self):
        with self.assertRaises(SystemExit):
            g.retry_call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))


class _Model:
    def __init__(self, name, actions):
        self.name = name
        self.supported_actions = actions


class _Client:
    def __init__(self, items):
        self._items = items

    @property
    def models(self):
        return self

    def list(self):
        return self._items


class ModelCandidateTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("TLDR_MODELS", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["TLDR_MODELS"] = self._env

    @staticmethod
    def _client():
        return _Client(
            [
                _Model("models/text-embedding-004", ["embedContent"]),
                _Model("models/gemini-2.5-flash", ["generateContent"]),
                _Model("models/gemini-3.6-flash", ["generateContent"]),
            ]
        )

    def test_requested_model_is_first_and_hardcoded_fallback_present(self):
        candidates = g.model_candidates(self._client(), "gemini-3.6-flash")
        self.assertEqual(candidates[0], "gemini-3.6-flash")
        self.assertIn("gemini-flash-lite-latest", candidates)
        self.assertIn("gemini-2.5-flash", candidates)
        self.assertNotIn("text-embedding-004", candidates)

    def test_env_models_are_honored(self):
        os.environ["TLDR_MODELS"] = "custom-model"
        candidates = g.model_candidates(_Client([]), "gemini-3.6-flash")
        self.assertIn("custom-model", candidates)


if __name__ == "__main__":
    unittest.main()
