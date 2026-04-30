#!/usr/bin/env python3
"""
wiki-entity-pages.py — synthesize per-topic wiki pages from existing
Topic-cluster source lists.

Reads the `🔖 Topic clusters` block of memory/index.md, parses each cluster
bullet into (entity name, source list), gathers source files, and asks
Claude Haiku to synthesize a `memory/wiki/<slug>.md` page.

Caching:
  Each entity's content hash (= sha256 of concatenated normalized source
  texts) is stored in `memory/wiki/.entity-cache.json`. If the hash hasn't
  changed since the last synthesis, the page is left alone.

Auth:
  Requires ANTHROPIC_API_KEY (source ~/.openclaw/env.sh).

Usage:
  python3 wiki-entity-pages.py                    # synthesize all clusters
  python3 wiki-entity-pages.py --only "raimd"     # one entity (substring match)
  python3 wiki-entity-pages.py --force            # ignore cache, re-synthesize all
  python3 wiki-entity-pages.py --dry-run          # show what would change
  python3 wiki-entity-pages.py --concurrency 4    # parallel synthesis
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("missing httpx: pip install --user httpx", file=sys.stderr)
    sys.exit(2)

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace")))
INDEX_PATH = WORKSPACE / "memory" / "index.md"
MEMORY_DIR = WORKSPACE / "memory"
SESSIONS_DIR = MEMORY_DIR / "sessions"
WIKI_DIR = MEMORY_DIR / "wiki"
WIKI_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = WIKI_DIR / ".entity-cache.json"
MEMORYMD_PATH = WORKSPACE / "MEMORY.md"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5"

# Bump when the prompt or output format changes; forces re-synthesis on next run.
# Recorded in the cache key so unchanged sources still re-synth when the prompt
# evolves. Bump history:
#   v1 — initial release
#   v2 — require named artifacts (PRs, releases) as their own bullets
PROMPT_VERSION = "v2"

SECTION_TOPICS = "## 🔖 Topic clusters"


def slugify_filename(name: str) -> str:
    """Convert an entity name into a wiki/<slug>.md filename.

    Strips markdown formatting (backticks, asterisks, etc.) but PRESERVES
    the content inside them. Drops parenthetical qualifiers (since they're
    usually descriptive sub-titles, not the entity's actual name).

    Examples:
      "`raimd` (C/C++ market data library)"  -> "raimd"
      "OpenCat: The Movie / Kling AI"        -> "opencat-the-movie-kling-ai"
      "Multi-machine setup (chex / frame ...)" -> "multi-machine-setup"
    """
    s = name
    # Drop parentheticals ("(C/C++ market data library)" etc.)
    s = re.sub(r"\([^)]*\)", "", s)
    # Strip markdown formatting markers but keep the inner text
    s = s.replace("`", "").replace("*", "").replace("_", "")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "untitled"


# ---------------------------------------------------------------------------
# Topic cluster parsing
# ---------------------------------------------------------------------------

# Match `- **<name>** — <body>` OR `- **[<name>](wiki/<slug>.md)** — <body>`.
# Accept both forms so the script keeps working after wiki-link-topics.py
# wraps cluster names in entity-page links.
CLUSTER_BULLET_RE = re.compile(
    r"^- \*\*(?P<name>(?:\[[^\]\n]+\]\(wiki/[^)\n]+\.md\)|[^*\n]+(?:`[^`\n]*`[^*\n]*)*))\*\* \u2014 (?P<body>.+)$",
    re.MULTILINE,
)

DAILY_LINK_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\](?:\(([^)]+)\))")
SESSION_LINK_RE = re.compile(r"\[`([0-9a-f-]{8,36})`\]\(sessions/([^)]+)\)")
MEMORYMD_LINK_RE = re.compile(r"\[`(MEMORY\.md(?:#[^`]*)?)`\]\(\.\.\/MEMORY\.md(?:#([^)]+))?\)")


def normalize_cluster_name(raw_name: str) -> str:
    """If the name is wrapped in a wiki-page link `[X](wiki/Y.md)`, return X.
    Otherwise return the name as-is. Used so the entity hash and prompts
    are stable regardless of whether wiki-link-topics.py has run yet.
    """
    m = re.match(r"^\[(.+?)\]\(wiki/[^)]+\.md\)$", raw_name.strip())
    if m:
        return m.group(1)
    return raw_name


def parse_topic_clusters(index_text: str) -> list[dict]:
    """Return a list of cluster dicts, each with:
        name, body, dailies, sessions, memorymd_anchors
    """
    # Find the topic-clusters block
    m = re.search(
        re.escape(SECTION_TOPICS) + r".*?(?=^## |\Z)",
        index_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not m:
        return []
    block = m.group(0)

    clusters = []
    for bm in CLUSTER_BULLET_RE.finditer(block):
        name = normalize_cluster_name(bm.group("name").strip())
        body = bm.group("body").strip()
        dailies = []
        for dm in DAILY_LINK_RE.finditer(body):
            date = dm.group(1)
            href = dm.group(2)
            dailies.append({"date": date, "href": href})
        sessions = []
        for sm in SESSION_LINK_RE.finditer(body):
            short_or_full = sm.group(1)
            href = sm.group(2)
            sessions.append({"id": short_or_full, "href": href})
        memorymd = []
        for mm in MEMORYMD_LINK_RE.finditer(body):
            memorymd.append({"label": mm.group(1), "anchor": mm.group(2) or ""})
        clusters.append({
            "name": name,
            "body": body,
            "dailies": dailies,
            "sessions": sessions,
            "memorymd": memorymd,
        })
    return clusters


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------

def read_file_safe(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def gather_sources(cluster: dict) -> dict[str, str]:
    """Return {label: text} for each source in the cluster.

    Daily files use their full body. Session sources use the per-session
    summary (`<uuid>.md`) — that's the LLM-curated q/a list, which is
    already a good distillation.
    """
    out: dict[str, str] = {}

    # Daily files
    for d in cluster["dailies"]:
        date = d["date"]
        path = MEMORY_DIR / f"{date}.md"
        text = read_file_safe(path)
        if text:
            out[f"daily:{date}"] = text

    # Session detail pages (the q/a summary)
    for s in cluster["sessions"]:
        sid = s["id"]
        # Resolve to full uuid filename
        if len(sid) == 8:
            # Find matching .md
            for p in SESSIONS_DIR.glob(f"{sid}*.md"):
                if not (p.name.endswith(".full.md") or p.name.endswith(".tools.md")):
                    text = read_file_safe(p)
                    if text:
                        out[f"session:{sid}"] = text
                    break
        else:
            path = SESSIONS_DIR / f"{sid}.md"
            text = read_file_safe(path)
            if text:
                out[f"session:{sid[:8]}"] = text

    # MEMORY.md sections
    if cluster["memorymd"]:
        memorymd_text = read_file_safe(MEMORYMD_PATH)
        for m in cluster["memorymd"]:
            anchor = m.get("anchor") or ""
            label = m["label"]
            # Try to extract the section under that anchor
            if anchor:
                # Find a heading whose slug matches anchor
                section_text = extract_memorymd_section(memorymd_text, anchor)
                if section_text:
                    out[f"memorymd:{label}"] = section_text
                    continue
            # Fallback: include the whole MEMORY.md
            out[f"memorymd:{label}"] = memorymd_text

    return out


def extract_memorymd_section(text: str, anchor_slug: str) -> str:
    """Find a heading whose slug matches anchor_slug, return that section's
    body (heading + content up to the next heading at the same or higher level).
    """
    lines = text.splitlines()
    target = None
    target_level = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", ln)
        if not m:
            continue
        heading = m.group(2)
        slug = re.sub(r"\s+", "-", heading.lower().strip())
        slug = re.sub(r"[^a-z0-9_\-]", "", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        if slug == anchor_slug:
            target = i
            target_level = len(m.group(1))
            break
    if target is None:
        return ""
    end = len(lines)
    for j in range(target + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= target_level:
            end = j
            break
    return "\n".join(lines[target:end]).strip() + "\n"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def content_hash(sources: dict[str, str], name: str) -> str:
    """Hash that determines whether the entity page needs re-synthesis.

    Includes the entity name (so renaming triggers a re-synth), the prompt
    version (so prompt evolution forces re-synth), and all source contents.
    Intentionally does NOT include the cluster body text from index.md — that
    gets rewritten by wiki-link-topics.py (e.g. wrapping the name in a
    wiki-page link), and we don't want such cosmetic changes to invalidate
    the cache.
    """
    h = hashlib.sha256()
    h.update(name.encode("utf-8"))
    h.update(b"\0")
    h.update(PROMPT_VERSION.encode("utf-8"))
    h.update(b"\0")
    for label in sorted(sources.keys()):
        h.update(label.encode("utf-8"))
        h.update(b"\0")
        h.update(sources[label].encode("utf-8"))
        h.update(b"\0\0")
    return h.hexdigest()


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE_PATH)


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a wiki maintainer synthesizing a Karpathy-style entity page from raw source notes.

Goal: produce a focused, present-tense reference page about ONE entity (a project, concept, system, person, or hardware item). Future-self should be able to read this page and answer "what is this thing, what's been done with it, and what's next" without re-reading the sources.

Format requirements (strict):
- H1 title: the entity name (use a clean, canonical form — drop noisy parens but keep technical qualifiers like (C/C++ market data library) when they're informative)
- A blockquote single-line elevator pitch (≤25 words) immediately after the title
- A status line: `**Status:** <active|dormant|completed|abandoned|in-progress> · **First mention:** YYYY-MM-DD · **Last mention:** YYYY-MM-DD`
- `## What it is` — 2-4 paragraphs of synthesis. Use present tense for things that exist now, past tense for events. Plain prose, no bullets.
- `## Key events / decisions` — bulleted, chronological, each bullet starts with `**YYYY-MM-DD** —`. Cite the source date in bold. Be specific about what happened or was decided. Skip trivia.
- `## Open questions / next steps` — only if the sources contain unresolved threads or stated next steps. Omit the section if everything is resolved.
- `## Cross-references` — list of `- related: <name>` lines for other entities mentioned (use plain text — the link script will turn them into links later). Skip if nothing relevant.

Style:
- Direct, declarative, no hedging. No "the user" or "Chris" — write in third person about the work, not the worker.
- Don't invent details. If a source is vague, leave the bullet vague.
- Don't quote the sources verbatim. Synthesize.
- No emoji. No markdown tables. No code blocks unless quoting a specific filename or shell command from a source.
- Output ONLY the markdown content of the page. No preamble, no "here's the page", no closing remarks.

NAMED ARTIFACTS — must appear in 'Key events / decisions':
When sources mention shipping/published artifacts — GitHub PRs (with PR numbers), upstream issues, releases, version tags, blog posts, public talks — include them as their own bullet with the identifier preserved (e.g., "PR #7596 (docs(gateway): add multi-machine config sync guide)"). These are durable historical anchors and must not be folded into a more general technical description.
"""


def build_user_prompt(name: str, body_hint: str, sources: dict[str, str]) -> str:
    parts = [
        f"ENTITY NAME: {name}",
        "",
        f"CLUSTER LINE (current rough description from index.md): {body_hint}",
        "",
        "SOURCES:",
        "",
    ]
    for label in sorted(sources.keys()):
        parts.append(f"--- BEGIN {label} ---")
        parts.append(sources[label].rstrip())
        parts.append(f"--- END {label} ---")
        parts.append("")
    parts.append("Synthesize the wiki page now. Output only the markdown content.")
    return "\n".join(parts)


async def call_haiku(client: httpx.AsyncClient, api_key: str, model: str,
                     name: str, body_hint: str, sources: dict[str, str],
                     retries: int = 3) -> tuple[str, dict]:
    user = build_user_prompt(name, body_hint, sources)
    body = {
        "model": model,
        "max_tokens": 2000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = await client.post(ANTHROPIC_URL, json=body, headers=headers, timeout=120.0)
            if r.status_code == 200:
                data = r.json()
                content = data.get("content") or []
                for c in content:
                    if c.get("type") == "text":
                        text = c.get("text", "").strip()
                        usage = data.get("usage") or {}
                        return text, usage
                return "", {}
            elif r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt)
                continue
            else:
                last_err = f"http {r.status_code}: {r.text[:300]}"
                break
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            last_err = str(e)
            await asyncio.sleep(2 ** attempt)
    print(f"  ! synthesis failed for {name}: {last_err}", file=sys.stderr)
    return "", {}


# ---------------------------------------------------------------------------
# Page rendering / file I/O
# ---------------------------------------------------------------------------

def render_page(name: str, slug: str, body_text: str,
                sources: dict[str, str], cluster: dict,
                gen_iso: str, content_sha: str) -> str:
    """Wrap the LLM-synthesized body with a sources footer."""
    src_lines = []
    for d in cluster["dailies"]:
        src_lines.append(f"- [daily {d['date']}](../{d['date']}.md)")
    for s in cluster["sessions"]:
        href = s["href"]
        src_lines.append(f"- [session `{s['id']}`](../sessions/{href})")
    for m in cluster["memorymd"]:
        anchor = m.get("anchor") or ""
        label = m["label"]
        if anchor:
            src_lines.append(f"- [`{label}`](../../MEMORY.md#{anchor})")
        else:
            src_lines.append(f"- [`{label}`](../../MEMORY.md)")

    footer = "\n".join([
        "",
        "---",
        "",
        f"**Sources synthesized as of {gen_iso[:10]}:**",
        "",
        *src_lines,
        "",
        f"_Last synthesized {gen_iso} by `scripts/wiki-entity-pages.py` "
        f"(content-hash `{content_sha[:12]}`)._",
    ])
    return body_text.rstrip() + "\n" + footer + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def synthesize_one(client: httpx.AsyncClient, api_key: str, model: str,
                         cluster: dict, cache: dict, force: bool,
                         dry_run: bool, sem: asyncio.Semaphore,
                         counters: dict) -> dict:
    name = cluster["name"]
    slug = slugify_filename(name)
    sources = gather_sources(cluster)
    if not sources:
        print(f"  - {name}: no sources found, skipping", file=sys.stderr)
        counters["skipped"] += 1
        return {"slug": slug, "name": name, "status": "skipped"}

    sha = content_hash(sources, name)
    cached = cache.get(slug, {})
    cached_sha = cached.get("content_sha", "")

    out_path = WIKI_DIR / f"{slug}.md"
    needs_synth = force or cached_sha != sha or not out_path.exists()
    if not needs_synth:
        print(f"  - {name}: cache hit ({slug}.md unchanged)", file=sys.stderr)
        counters["cache_hits"] += 1
        return {"slug": slug, "name": name, "status": "cached"}

    if dry_run:
        print(f"  ~ {name}: would synthesize {slug}.md (sources: {len(sources)})", file=sys.stderr)
        counters["dry_run"] += 1
        return {"slug": slug, "name": name, "status": "would-synth"}

    async with sem:
        text, usage = await call_haiku(client, api_key, model, name, cluster["body"], sources)

    if not text:
        counters["failures"] += 1
        return {"slug": slug, "name": name, "status": "failed"}

    gen_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    page = render_page(name, slug, text, sources, cluster, gen_iso, sha)
    out_path.write_text(page, encoding="utf-8")

    cache[slug] = {
        "name": name,
        "content_sha": sha,
        "synthesized_at": gen_iso,
        "model": model,
        "source_count": len(sources),
        "tokens": usage,
    }

    print(f"  + {name}: wrote {slug}.md ({len(page)} bytes, "
          f"in={usage.get('input_tokens', 0)}, out={usage.get('output_tokens', 0)})",
          file=sys.stderr)
    counters["fresh"] += 1
    return {"slug": slug, "name": name, "status": "fresh"}


async def main_async(args):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        print("ANTHROPIC_API_KEY not set. Source ~/.openclaw/env.sh first.", file=sys.stderr)
        sys.exit(2)

    if not INDEX_PATH.exists():
        print(f"missing {INDEX_PATH}; run wiki-compose.py first.", file=sys.stderr)
        sys.exit(2)

    index_text = INDEX_PATH.read_text(encoding="utf-8")
    clusters = parse_topic_clusters(index_text)
    if not clusters:
        print("No topic clusters found in index.md", file=sys.stderr)
        return

    if args.only:
        needle = args.only.lower()
        clusters = [c for c in clusters if needle in c["name"].lower() or needle in slugify_filename(c["name"])]
        if not clusters:
            print(f"No clusters matched filter {args.only!r}", file=sys.stderr)
            return

    cache = load_cache()
    counters = {"fresh": 0, "cache_hits": 0, "skipped": 0, "failures": 0, "dry_run": 0}

    sem = asyncio.Semaphore(args.concurrency)
    print(f"Processing {len(clusters)} cluster(s) "
          f"(concurrency={args.concurrency}, model={args.model}, force={args.force}, dry_run={args.dry_run})...",
          file=sys.stderr)

    async with httpx.AsyncClient() as client:
        tasks = [
            synthesize_one(client, api_key or "", args.model, c, cache,
                          args.force, args.dry_run, sem, counters)
            for c in clusters
        ]
        await asyncio.gather(*tasks)

    if not args.dry_run:
        save_cache(cache)

    print(f"\nDone. fresh={counters['fresh']} cache={counters['cache_hits']} "
          f"skipped={counters['skipped']} failed={counters['failures']} "
          f"dry_run={counters['dry_run']}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--only", default="", help="Substring match on entity name or slug")
    ap.add_argument("--force", action="store_true", help="Ignore cache, re-synthesize all")
    ap.add_argument("--dry-run", action="store_true", help="Print what would happen, don't write")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
