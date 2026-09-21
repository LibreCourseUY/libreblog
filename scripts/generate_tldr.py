#!/usr/bin/env python3
"""Generate the weekly LibreCourseUY TL;DR.

Pulls the last week of tech news from a curated RSS list, asks Gemini to write
an edition in the house style, validates it, and writes it to
``content/tldr/vX.Y.md``. The GitHub workflow commits it and opens a PR.

Usage:
    GEMINI_API_KEY=... python scripts/generate_tldr.py
    python scripts/generate_tldr.py --dry-run --since-days 7
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
TLDR_DIR = REPO_ROOT / "content" / "tldr"
STYLE_FILE = REPO_ROOT / "scripts" / "tldr_style.md"
FEEDS_FILE = REPO_ROOT / "scripts" / "feeds.txt"

USER_AGENT = "libreblog-tldr/1.0 (+https://github.com/LibreCourseUY/libreblog)"
DEFAULT_MODEL = "gemini-3.6-flash"
MAX_ITEMS = 60
PER_FEED = 8
MAX_SUMMARY = 400
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.md$")
DATE_RE = re.compile(r"^date:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def log(message: str) -> None:
    print(message, file=sys.stderr)


def read_feeds() -> list[str]:
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            feeds.append(line)
    return feeds


def strip_html(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_SUMMARY]


def entry_datetime(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def normalize_url(url: str) -> str:
    return re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/").lower()


def normalize_title(title: str) -> str:
    return re.sub(r"\W+", " ", (title or "").lower()).strip()


def fetch_feed(client: httpx.Client, url: str, window_start: datetime) -> list[dict]:
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as error:
        log(f"warn: could not fetch {url}: {error}")
        return []

    feed = feedparser.parse(response.content)
    source = (feed.feed.get("title") or httpx.URL(url).host or url).strip()
    articles = []

    for entry in feed.entries:
        published = entry_datetime(entry)
        if published is None or published < window_start:
            continue
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        articles.append(
            {
                "source": source,
                "title": title,
                "url": link,
                "published": published,
                "summary": strip_html(entry.get("summary") or entry.get("description") or ""),
            }
        )

    articles.sort(key=lambda article: article["published"], reverse=True)
    return articles[:PER_FEED]


def collect_articles(since_days: int) -> list[dict]:
    window_start = datetime.now(timezone.utc) - timedelta(days=since_days)
    feeds = read_feeds()
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, */*"},
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(lambda url: fetch_feed(client, url, window_start), feeds)
            articles = [article for batch in results for article in batch]

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique = []
    for article in sorted(articles, key=lambda item: item["published"], reverse=True):
        url_key = normalize_url(article["url"])
        title_key = normalize_title(article["title"])
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        unique.append(article)

    return unique[:MAX_ITEMS]


def next_version() -> str:
    highest = (0, 0)
    for path in TLDR_DIR.glob("v*.md"):
        match = VERSION_RE.match(path.name)
        if not match:
            continue
        candidate = (int(match.group(1)), int(match.group(2)))
        highest = max(highest, candidate)
    return f"v{highest[0]}.{highest[1] + 1}"


def recent_post_exists(since_days: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
    for path in TLDR_DIR.glob("v*.md"):
        match = DATE_RE.search(path.read_text(encoding="utf-8"))
        if not match:
            continue
        if datetime.strptime(match.group(1), "%Y-%m-%d").date() >= cutoff:
            log(f"info: {path.name} was already generated for this week")
            return True
    return False


def reference_editions(limit: int = 3) -> str:
    paths = sorted(
        (path for path in TLDR_DIR.glob("v*.md") if VERSION_RE.match(path.name)),
        key=lambda path: tuple(int(part) for part in VERSION_RE.match(path.name).groups()),
        reverse=True,
    )[:limit]
    blocks = []
    for path in paths:
        blocks.append(f"--- reference {path.name} ---\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(blocks)


def build_prompt(articles: list[dict], version: str, since_days: int) -> str:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=since_days)).date().isoformat()
    lines = [
        f"Today is {now.date().isoformat()}.",
        f"Write edition {version} covering the week {start} to {now.date().isoformat()}.",
        "",
        "Reference editions (match the tone and structure, not the content):",
        reference_editions(),
        "",
        "Articles from the last week. Use ONLY these and link them inline:",
    ]
    for index, article in enumerate(articles, start=1):
        published = article["published"].date().isoformat()
        lines.append(f"{index}. [{article['source']} | {published}] {article['title']}")
        lines.append(f"   {article['url']}")
        if article["summary"]:
            lines.append(f"   {article['summary']}")
    lines.append("")
    lines.append(f"Now return the full Markdown file for {version}, and nothing else.")
    return "\n".join(lines)


def generate(articles: list[dict], version: str, model: str, since_days: int) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")

    from google import genai
    from google.genai import types

    style = STYLE_FILE.read_text(encoding="utf-8")
    prompt = build_prompt(articles, version, since_days)
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=style, temperature=0.7),
    )
    text = (response.text or "").strip()
    if not text:
        raise SystemExit("Gemini returned an empty response")
    return text


def strip_code_fence(markdown: str) -> str:
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", markdown.strip(), re.DOTALL)
    return match.group(1).strip() if match else markdown


def sanitize(markdown: str) -> str:
    text = strip_code_fence(markdown.replace("\r\n", "\n").strip())
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = re.sub(r"\s+--\s+", " - ", text)
    return text.rstrip() + "\n"


def validate(markdown: str, version: str) -> None:
    problems = []
    if not markdown.startswith("---\n"):
        problems.append("missing YAML front matter")
    if not re.search(rf"^title:\s*{re.escape(version)}\s*$", markdown, re.MULTILINE):
        problems.append(f"front matter does not declare title: {version}")
    if not re.search(r"^date:\s*\d{4}-\d{2}-\d{2}\s*$", markdown, re.MULTILINE):
        problems.append("front matter is missing a date")
    if "### Tech News" not in markdown:
        problems.append("missing '### Tech News' section")
    if len(re.findall(r"^####\s+", markdown, re.MULTILINE)) < 2:
        problems.append("needs at least two topic subsections")
    if len(re.findall(r"\]\(https?://", markdown)) < 3:
        problems.append("needs at least three source links")
    if "- The Editor" not in markdown:
        problems.append("missing the '- The Editor' footer")
    if "\u2014" in markdown or "\u2013" in markdown or re.search(r"\s--\s", markdown):
        problems.append("contains a forbidden dash")
    if problems:
        raise SystemExit("generated TL;DR failed validation: " + "; ".join(problems))


def set_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    log(f"output: {name}={value}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the weekly TL;DR.")
    parser.add_argument("--since-days", type=int, default=7, help="days of news to include")
    parser.add_argument("--model", default=os.environ.get("TLDR_MODEL", DEFAULT_MODEL))
    parser.add_argument("--dry-run", action="store_true", help="print the draft, write nothing")
    parser.add_argument("--force", action="store_true", help="ignore the weekly skip check")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.force and recent_post_exists(args.since_days):
        log("nothing to do")
        return 0

    articles = collect_articles(args.since_days)
    log(f"collected {len(articles)} articles from the last {args.since_days} days")
    if len(articles) < 5:
        raise SystemExit("not enough articles to write a TL;DR")

    version = next_version()
    markdown = sanitize(generate(articles, version, args.model, args.since_days))
    validate(markdown, version)

    if args.dry_run:
        print(markdown)
        return 0

    TLDR_DIR.mkdir(parents=True, exist_ok=True)
    path = TLDR_DIR / f"{version}.md"
    path.write_text(markdown, encoding="utf-8")
    try:
        relative = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        relative = path.as_posix()
    log(f"wrote {relative}")
    set_output("version", version)
    set_output("file", relative)
    return 0


if __name__ == "__main__":
    sys.exit(main())
