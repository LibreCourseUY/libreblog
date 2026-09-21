# libreblog

Blog for LibreCourseUY, built with [Hugo](https://gohugo.io/) and the
PaperMod theme, and auto-deployed via GitHub Actions. Every push to `main`
builds a Docker image and publishes it to GHCR.

## Local development

```bash
git clone --recurse-submodules https://github.com/LibreCourseUY/libreblog
cd libreblog
hugo server
```

The site is generated from `content/` and configured in `hugo.yaml`.

## Weekly TL;DR automation

A GitHub Actions workflow (`.github/workflows/weekly-tldr.yml`) runs every
Monday at 12:00 UTC. It:

1. pulls the last week of tech news from the RSS feeds in `scripts/feeds.txt`;
2. asks Gemini to write an edition in the house style (`scripts/tldr_style.md`,
   using the latest editions as examples) with exactly 14 news items;
3. validates the result (front matter, sections, source links, footer, no
   em dashes, no double hyphens) and writes `content/tldr/vX.Y.md`;
4. opens a pull request. Merge it to publish.

### Setup

- Add the repository secret `GEMINI_API_KEY`.
- Enable Settings -> Actions -> "Allow GitHub Actions to create and approve
  pull requests".

### Running it by hand

```bash
python -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt

# print a draft without writing or opening a PR
GEMINI_API_KEY=... .venv/bin/python scripts/generate_tldr.py --dry-run

# write the file locally (no commit, no PR)
GEMINI_API_KEY=... .venv/bin/python scripts/generate_tldr.py
```

Useful flags: `--since-days N`, `--model gemini-flash-lite-latest`, `--force`.

The generator retries transient Gemini errors and falls back to other
available models when the requested one is overloaded. Set `TLDR_MODELS` to a
comma-separated list to control the fallback order.

The workflow also supports manual dispatch with the `dry_run` and `since_days`
inputs.

### Tests

```bash
.venv/bin/python -m unittest scripts.test_generate_tldr
```

## License

MIT, see [LICENSE](LICENSE).
