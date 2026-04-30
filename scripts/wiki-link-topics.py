#!/usr/bin/env python3
"""
wiki-link-topics.py — add links to mentions of dates, session UUIDs, and
MEMORY.md headings inside the editorial sections of index.md.

The composer preserves these editorial blocks verbatim across regens:
  - 🪞 About this index
  - 🔖 Topic clusters

So any links we add here will survive the next composer run. The script is
idempotent: it skips text inside existing markdown links / fenced code
blocks. Inline code spans are candidates for rewriting (that's how we turn
a backticked uuid like ``abc12345`` into a clickable session link), but
once rewritten the new link becomes protected on subsequent runs.

What it links:
  - YYYY-MM-DD (bare or bold)              → memory/YYYY-MM-DD.md (only if file exists)
  - session `<8-char-prefix>` / `<full UUID>` → sessions/<full-uuid>.md (only if file exists)
  - `MEMORY.md#<heading-or-anchor>`         → ../MEMORY.md[#anchor]
  - bare MEMORY.md                          → ../MEMORY.md

Usage:
  python3 wiki-link-topics.py                    # rewrites memory/index.md in place
  python3 wiki-link-topics.py --dry-run          # show diff, don't write
  python3 wiki-link-topics.py --section topics   # only the Topic clusters block
  python3 wiki-link-topics.py --section about    # only the About block
  python3 wiki-link-topics.py --section both     # default
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace")))
INDEX_PATH = WORKSPACE / "memory" / "index.md"
MEMORY_DIR = WORKSPACE / "memory"
SESSIONS_DIR = MEMORY_DIR / "sessions"
WIKI_DIR = MEMORY_DIR / "wiki"
MEMORYMD_PATH = WORKSPACE / "MEMORY.md"

SECTION_ABOUT = "## 🪞 About this index"
SECTION_DAILIES = "## 📅 Daily logs"
SECTION_TOPICS = "## 🔖 Topic clusters"


def slugify(heading: str) -> str:
    s = heading.lower()
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def discover_files() -> tuple[set[str], dict[str, str], dict[str, str], set[str]]:
    """Return (daily_dates, session_short_to_full, memorymd_heading_to_slug,
    wiki_slugs)."""
    daily_dates: set[str] = set()
    if MEMORY_DIR.exists():
        for p in MEMORY_DIR.glob("*.md"):
            stem = p.stem
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
                daily_dates.add(stem)

    session_short: dict[str, str] = {}
    if SESSIONS_DIR.exists():
        for p in SESSIONS_DIR.glob("*.md"):
            stem = p.stem
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", stem):
                session_short[stem[:8]] = stem

    memorymd_anchors: dict[str, str] = {}
    if MEMORYMD_PATH.exists():
        for line in MEMORYMD_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if m:
                heading = m.group(1)
                memorymd_anchors[heading] = slugify(heading)

    wiki_slugs: set[str] = set()
    if WIKI_DIR.exists():
        for p in WIKI_DIR.glob("*.md"):
            wiki_slugs.add(p.stem)

    return daily_dates, session_short, memorymd_anchors, wiki_slugs


def split_protected_links_only(text: str) -> list[tuple[str, bool]]:
    """Split text into segments where existing markdown links and fenced code
    blocks are protected. Inline code spans (`…`) are NOT protected because
    we want to rewrite code-span UUIDs / MEMORY.md refs into links.
    """
    n = len(text)
    protected: list[tuple[int, int]] = []

    for m in re.finditer(r"```.*?```", text, flags=re.DOTALL):
        protected.append((m.start(), m.end()))
    for m in re.finditer(r"!?\[[^\]\n]*\]\([^)\n]+\)", text):
        protected.append((m.start(), m.end()))

    protected.sort()
    merged: list[tuple[int, int]] = []
    for start, end in protected:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    segments: list[tuple[str, bool]] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            segments.append((text[cursor:start], False))
        segments.append((text[start:end], True))
        cursor = end
    if cursor < n:
        segments.append((text[cursor:n], False))
    return segments


def slugify_entity_name(name: str) -> str:
    """Same algorithm as wiki-entity-pages.py slugify_filename. Used to
    detect when a topic-cluster bullet has a corresponding wiki/<slug>.md
    page so we can prepend a link to it.
    """
    s = re.sub(r"\([^)]*\)", "", name)
    s = s.replace("`", "").replace("*", "").replace("_", "")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def link_topic_cluster_bullets(text: str, wiki_slugs: set[str]) -> str:
    r"""Convert each bullet ``- **<name>** — <body> *(future: `wiki/X.md`)*``
    into ``- **[<name>](wiki/<slug>.md)** — <body>`` when wiki/<slug>.md
    exists.

    Idempotent: if the bullet's name is already wrapped in a link, leave it.
    Also strips the now-redundant ``*(future: `wiki/X.md`)*`` suffix when
    the page exists, plus any trailing `·` separator that's left dangling.
    """
    lines = text.splitlines(keepends=True)
    out_lines = []
    bullet_re = re.compile(r"^- \*\*((?:[^*]|`[^`]*`)+)\*\* — (.+)$")
    linked_re = re.compile(r"^- \*\*\[(?:[^\]]+)\]\([^)]+\)\*\* — ")
    future_suffix_re = re.compile(r"\s*\*\(future: `wiki/[^`]+\.md`\)\*\s*$")
    trailing_sep_re = re.compile(r"\s*·\s*$")

    def clean_body(body: str) -> str:
        body = future_suffix_re.sub("", body)
        body = trailing_sep_re.sub("", body)
        return body.rstrip()

    for line in lines:
        # Already linked? Just clean residual suffix/separator and keep.
        if linked_re.match(line):
            stripped = line.rstrip("\n")
            cleaned = clean_body(stripped)
            out_lines.append(cleaned + "\n")
            continue
        m = bullet_re.match(line.rstrip("\n"))
        if not m:
            out_lines.append(line)
            continue
        name = m.group(1)
        body = m.group(2)
        slug = slugify_entity_name(name)
        if slug and slug in wiki_slugs:
            body = clean_body(body)
            out_lines.append(f"- **[{name}](wiki/{slug}.md)** — {body}\n")
        else:
            out_lines.append(line)
    return "".join(out_lines)


def transform_unprotected(seg: str, daily_dates: set[str],
                          session_short: dict[str, str],
                          memorymd_anchors: dict[str, str]) -> str:
    """Apply link substitutions to a chunk of text that is guaranteed to NOT
    overlap any existing markdown link or fenced code block.
    """
    # 1. Backticked session uuids → session links
    def session_sub(m: re.Match) -> str:
        full = m.group(0)
        inner = full[1:-1]
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", inner):
            short = inner[:8]
            if short in session_short:
                return f"[`{inner}`](sessions/{session_short[short]}.md)"
        if re.fullmatch(r"[0-9a-f]{8}", inner):
            if inner in session_short:
                return f"[`{inner}`](sessions/{session_short[inner]}.md)"
        return full
    seg = re.sub(r"`[^`\n]+`", session_sub, seg)

    # 2. Backticked MEMORY.md / MEMORY.md#... → memory file links
    def memorymd_sub(m: re.Match) -> str:
        full = m.group(0)
        inner = full[1:-1]
        if inner == "MEMORY.md":
            return "[`MEMORY.md`](../MEMORY.md)"
        if inner.startswith("MEMORY.md#"):
            anchor_or_heading = inner[len("MEMORY.md#"):]
            if anchor_or_heading in memorymd_anchors:
                slug = memorymd_anchors[anchor_or_heading]
            else:
                slug = slugify(anchor_or_heading)
            return f"[`MEMORY.md#{anchor_or_heading}`](../MEMORY.md#{slug})"
        return full
    seg = re.sub(r"`[^`\n]+`", memorymd_sub, seg)

    # 3. Bare "MEMORY.md" (no backticks, not part of a larger word/path)
    seg = re.sub(
        r"(?<![`\[/\.\w])MEMORY\.md(?![`\w])",
        "[`MEMORY.md`](../MEMORY.md)",
        seg,
    )

    # 4. Dates YYYY-MM-DD → daily-file links (only if file exists)
    def date_sub(m: re.Match) -> str:
        date = m.group(1)
        if date not in daily_dates:
            return m.group(0)
        return f"[{date}]({date}.md)"
    seg = re.sub(
        r"(?<![\w/\[])(\d{4}-\d{2}-\d{2})(?![\w/\)])",
        date_sub,
        seg,
    )
    return seg


def link_section_text(text: str, daily_dates: set[str],
                      session_short: dict[str, str],
                      memorymd_anchors: dict[str, str]) -> str:
    out_parts: list[str] = []
    for seg, is_protected in split_protected_links_only(text):
        if is_protected:
            out_parts.append(seg)
        else:
            out_parts.append(transform_unprotected(
                seg, daily_dates, session_short, memorymd_anchors
            ))
    return "".join(out_parts)


def find_section(text: str, anchor: str, end_anchors: list[str]) -> tuple[int, int] | None:
    """Return (start_line_idx, end_line_idx) for the section."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(anchor):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        for ea in end_anchors:
            if lines[j].startswith(ea):
                end = j
                break
        if end != len(lines):
            break
    return (start, end)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--section", choices=["topics", "about", "both"], default="both")
    ap.add_argument("--path", default=str(INDEX_PATH))
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"missing {path}", file=sys.stderr)
        sys.exit(1)

    daily_dates, session_short, memorymd_anchors, wiki_slugs = discover_files()
    text = path.read_text(encoding="utf-8")

    sections_to_link: list[tuple[str, list[str]]] = []
    if args.section in ("topics", "both"):
        sections_to_link.append((SECTION_TOPICS, []))
    if args.section in ("about", "both"):
        sections_to_link.append((SECTION_ABOUT, [SECTION_DAILIES]))

    lines = text.splitlines(keepends=True)
    new_lines = list(lines)

    total_added = 0
    for anchor, end_anchors in sections_to_link:
        bounds = find_section(text, anchor, end_anchors)
        if bounds is None:
            print(f"  section {anchor!r}: not found, skipping", file=sys.stderr)
            continue
        s, e = bounds
        original = "".join(lines[s:e])
        # Topic-cluster section also gets the entity-page link pass
        if anchor == SECTION_TOPICS:
            stage1 = link_topic_cluster_bullets(original, wiki_slugs)
        else:
            stage1 = original
        rewritten = link_section_text(
            stage1, daily_dates, session_short, memorymd_anchors
        )
        new_lines[s:e] = [rewritten]
        added = rewritten.count("](") - original.count("](")
        total_added += added
        print(f"  section {anchor!r}: +{added} link(s)", file=sys.stderr)

    new_text = "".join(new_lines)
    if new_text == text:
        print("(no changes)", file=sys.stderr)
        return

    if args.dry_run:
        import difflib
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(path),
            tofile="(after link pass)",
        ))
        sys.stdout.write(diff)
    else:
        path.write_text(new_text, encoding="utf-8")
        print(f"wrote {path} (+{total_added} link(s) total)", file=sys.stderr)


if __name__ == "__main__":
    main()
