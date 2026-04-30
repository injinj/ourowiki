#!/usr/bin/env python3
"""
wiki-cross-refs.py — resolve `- related: <name>` lines in wiki entity pages
to links, and inject auto-generated `## Backlinks` sections.

Layer 1 of the Karpathy-wiki entity-linking work. Idempotent.

What it does:
  1. Parses every `memory/wiki/<slug>.md` to extract entity name + the
     `- related: <name>` lines from the `## Cross-references` section.
  2. Builds a name -> slug alias map (slug, H1 title, cluster name from
     index.md, plus token-level aliases like "raims" -> the page that
     declares it as its primary topic).
  3. For each page's Cross-references section, rewrites resolvable lines
     as `- related: [<original>](<slug>.md)`. Skips already-linked names.
  4. Builds a reverse map: for each page, which other pages now link to it?
  5. Injects (or updates) a `## Backlinks` section right before the
     `---` sources footer, listing those pages in alphabetical order.
     Idempotent: byte-identical when the backlink set hasn't changed.

Does NOT modify:
  - The synthesized body of each page (LLM-authored content above
    Cross-references stays untouched).
  - The sources footer (everything after the final `---`).
  - The entity-page content hash cache (this script doesn't touch the cache;
    the cache is over inputs, not output files).

Usage:
  python3 wiki-cross-refs.py             # run, write all changes
  python3 wiki-cross-refs.py --dry-run   # show diff per file, don't write
  python3 wiki-cross-refs.py --report    # print resolution report (which
                                          # related names map where, and
                                          # which couldn't be resolved)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace")))
WIKI_DIR = WORKSPACE / "memory" / "wiki"
INDEX_PATH = WORKSPACE / "memory" / "index.md"

SECTION_CROSS_REFS = "## Cross-references"
SECTION_BACKLINKS = "## Backlinks"


def slugify(s: str) -> str:
    """Normalize a name for fuzzy comparison: lowercase alnum + hyphens."""
    s = re.sub(r"\([^)]*\)", "", s)  # drop parens
    s = s.replace("`", "").replace("*", "").replace("_", "")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------------------
# Wiki page discovery + parsing
# ---------------------------------------------------------------------------


def discover_pages() -> dict[str, dict]:
    """Return {slug: {path, h1_title, related_lines, body_text}} for every
    `wiki/<slug>.md` file."""
    out: dict[str, dict] = {}
    if not WIKI_DIR.exists():
        return out
    for p in sorted(WIKI_DIR.glob("*.md")):
        if p.name.startswith("."):
            continue
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^# (.+?)\s*$", text, re.MULTILINE)
        h1 = m.group(1).strip() if m else p.stem
        out[p.stem] = {
            "path": p,
            "h1_title": h1,
            "text": text,
        }
    return out


def parse_index_cluster_names() -> dict[str, str]:
    """Read index.md's Topic clusters block to map slug -> cluster name.

    Cluster bullets look like:
      - **[<name>](wiki/<slug>.md)** — <body>
    """
    out: dict[str, str] = {}
    if not INDEX_PATH.exists():
        return out
    text = INDEX_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^- \*\*\[((?:[^\]]|\\\])+)\]\(wiki/([^)]+)\.md\)\*\* \u2014",
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        name = m.group(1)
        slug = m.group(2)
        out[slug] = name
    return out


# ---------------------------------------------------------------------------
# Alias map
# ---------------------------------------------------------------------------


# Tokens that, when matched alone, point at a specific page. The token is the
# *primary subject* of the target page.
PRIMARY_TOKENS: dict[str, str] = {
    # populated by build_alias_map() based on parsing
}


def build_alias_map(pages: dict[str, dict],
                    index_names: dict[str, str]) -> dict[str, str]:
    """Build a map of normalized-name-string -> canonical slug.

    Aliases come from:
      - The page's slug itself
      - The page's H1 title
      - The cluster name from index.md
      - A small set of hand-picked primary tokens (raims -> networking page,
        raimd -> raimd page, etc.). These cover cases where a one-word token
        in a related: line should resolve to the page that primarily covers
        that token.
    """
    aliases: dict[str, str] = {}

    def add(key: str, slug: str) -> None:
        norm = slugify(key)
        if not norm:
            return
        # First-write-wins so explicit aliases below override implicit ones
        if norm not in aliases:
            aliases[norm] = slug

    # Slugs and H1s
    for slug, info in pages.items():
        add(slug, slug)
        add(info["h1_title"], slug)

    # Cluster names from index.md
    for slug, name in index_names.items():
        if slug in pages:
            add(name, slug)

    # Hand-picked one-word primary-subject tokens. These resolve a bare
    # token to the page that's primarily about it. Order doesn't matter
    # because aliases is first-write-wins, but slug/title aliases above
    # already win when they match.
    #
    # NOTE FOR FORKERS: this dict is the primary place where the linker
    # gets customized for a specific corpus. The entries below are the
    # original author's project tokens (raimd, chex, dyna, ...). Replace
    # them with your own one-word tokens and target slugs. Empty dict is
    # fine if you only have multi-word entity names.
    primary = {
        "raims": "networking-multicast-rvd-raims",
        "rvd": "networking-multicast-rvd-raims",
        "raicache": "networking-multicast-rvd-raims",
        "frr": "networking-multicast-rvd-raims",
        "dnsmasq": "networking-multicast-rvd-raims",
        "bind": "networking-multicast-rvd-raims",
        "systemd-nspawn": "networking-multicast-rvd-raims",
        "raimd": "raimd",
        "mdmsgwriter": "raimd",
        "mdfielditer": "raimd",
        "rvmsg": "raimd",
        "rwfmsgwriter": "raimd",
        "tibmsgwriter": "raimd",
        "tibsassmsgwriter": "raimd",
        "jsonmsgwriter": "raimd",
        "rwffieldlistwriter": "raimd",
        "chex": "chex-hardware",
        "dyna": "multi-machine-setup",
        "pumpkin": "multi-machine-setup",
        "frame": "multi-machine-setup",
        "kling": "opencat-the-movie-kling-ai",
        "qwen": "llm-model-infrastructure-and-inference",
        "qwen2-5-coder": "llm-model-infrastructure-and-inference",
        "llama-cpp": "llm-model-infrastructure-and-inference",
        "aider": "llm-model-infrastructure-and-inference",
        "openclaw": "openclaw-source-build-contribution-workflow",
        "wiki-maintainer-skill": "openclaw-source-build-contribution-workflow",
    }
    for token, slug in primary.items():
        if slug in pages:
            add(token, slug)

    return aliases


# ---------------------------------------------------------------------------
# Cross-references rewriting
# ---------------------------------------------------------------------------


RELATED_LINE_RE = re.compile(
    r"^(\s*)- related:\s*(.+?)\s*$",
    re.MULTILINE,
)
ALREADY_LINKED_RE = re.compile(r"^\[[^\]]+\]\([^)]+\)$")


RESOLVED = "resolved"
SELF_REF = "self-reference"
NO_MATCH = "no-match"


def resolve_related(name: str, aliases: dict[str, str], self_slug: str) -> tuple[str, str | None]:
    """Return (status, target_slug). status is one of RESOLVED, SELF_REF, NO_MATCH.
    target_slug is the slug if status==RESOLVED else None.
    """
    norm = slugify(name)
    if not norm:
        return (NO_MATCH, None)
    target = aliases.get(norm)
    if target is None:
        return (NO_MATCH, None)
    if target == self_slug:
        return (SELF_REF, None)
    return (RESOLVED, target)


def rewrite_cross_refs_section(text: str, slug: str,
                               aliases: dict[str, str],
                               counters: dict[str, int]) -> tuple[str, set[str]]:
    """Find the Cross-references section in `text`, rewrite resolvable
    `- related:` lines to links, and return (new_text, set_of_target_slugs).

    target_slugs is what gets fed to the backlinks pass.
    """
    # Find section bounds. Section starts at line beginning with
    # SECTION_CROSS_REFS, ends at the next H2 or the final `---` separator.
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(SECTION_CROSS_REFS):
            start = i
            break
    if start is None:
        return text, set()

    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j].rstrip("\n")
        if ln.startswith("## ") or ln == "---":
            end = j
            break

    targets: set[str] = set()
    out_lines = list(lines)
    for k in range(start, end):
        ln = lines[k]
        m = RELATED_LINE_RE.match(ln.rstrip("\n"))
        if not m:
            continue
        indent, rest = m.group(1), m.group(2)
        # Already linked? extract the slug if it points at a wiki page.
        if ALREADY_LINKED_RE.match(rest):
            link_m = re.match(r"^\[[^\]]+\]\(([^)]+)\)$", rest)
            if link_m:
                href = link_m.group(1)
                if href.endswith(".md"):
                    href_slug = href[:-3]
                    href_slug = href_slug.split("/")[-1]
                    if href_slug != slug:
                        targets.add(href_slug)
                        counters["already_linked"] += 1
            continue
        status, target = resolve_related(rest, aliases, slug)
        if status == RESOLVED:
            targets.add(target)
            new_ln = f"{indent}- related: [{rest}]({target}.md)\n"
            out_lines[k] = new_ln
            counters["resolved"] += 1
        elif status == SELF_REF:
            counters["self_ref"] += 1
        else:
            counters["no_match"] += 1

    return "".join(out_lines), targets


# ---------------------------------------------------------------------------
# Backlinks injection
# ---------------------------------------------------------------------------


def render_backlinks_section(backlinks: list[tuple[str, str]]) -> str:
    """`backlinks` is a list of (source_slug, source_title) tuples. Render
    the `## Backlinks` section."""
    if not backlinks:
        return ""
    lines = [SECTION_BACKLINKS, "", "_Pages that link here (auto-generated):_", ""]
    seen: set[str] = set()
    for slug, title in sorted(backlinks, key=lambda t: t[1].lower()):
        if slug in seen:
            continue
        seen.add(slug)
        lines.append(f"- [{title}]({slug}.md)")
    lines.append("")
    return "\n".join(lines)


def inject_backlinks(text: str, backlinks_section: str) -> str:
    """Insert or replace the `## Backlinks` section.

    Layout (canonical, idempotent):
      ...end of last existing section content (no trailing blank lines)
      <blank line>
      ## Backlinks
      ...backlinks bullets...
      <blank line>
      ---
      ...sources footer...
    """
    text = strip_backlinks(text)

    if not backlinks_section:
        return text

    lines = text.splitlines(keepends=True)
    final_hr = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip("\n") == "---":
            final_hr = i
            break

    # Normalize the section text: ensure it ends without trailing blanks.
    section_text = backlinks_section.rstrip() + "\n"

    if final_hr is None:
        # No sources footer? Append at end.
        body = text.rstrip() + "\n\n"
        return body + section_text + "\n"

    # Walk back from final_hr to skip any blank lines preceding `---`.
    j = final_hr
    while j > 0 and lines[j - 1].strip() == "":
        j -= 1
    body_part = "".join(lines[:j]).rstrip() + "\n"
    footer_part = "".join(lines[final_hr:])
    return body_part + "\n" + section_text + "\n" + footer_part


def strip_backlinks(text: str) -> str:
    """Remove an existing `## Backlinks` section AND any blank lines that
    immediately precede or follow it. Idempotent."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(SECTION_BACKLINKS):
            start = i
            break
    if start is None:
        return text
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j].rstrip("\n")
        if ln.startswith("## ") or ln == "---":
            end = j
            break
    # Also strip blank lines BEFORE the section heading (they'll be re-added
    # if we re-inject) and AFTER the section content.
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    while end < len(lines) and lines[end].strip() == "":
        end += 1
    return "".join(lines[:start] + lines[end:])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="Print resolution report (resolved / unresolved counts per page)")
    args = ap.parse_args()

    pages = discover_pages()
    if not pages:
        print(f"No wiki pages found in {WIKI_DIR}", file=sys.stderr)
        return

    index_names = parse_index_cluster_names()
    aliases = build_alias_map(pages, index_names)

    # Pass 1: rewrite cross-references in each page, collect outgoing target sets.
    outgoing: dict[str, set[str]] = {}
    rewritten_text: dict[str, str] = {}
    counters = {"resolved": 0, "already_linked": 0, "self_ref": 0, "no_match": 0}
    self_ref_by_page: dict[str, list[str]] = defaultdict(list)
    no_match_by_page: dict[str, list[str]] = defaultdict(list)

    for slug, info in pages.items():
        text = info["text"]
        new_text, targets = rewrite_cross_refs_section(text, slug, aliases, counters)
        outgoing[slug] = targets
        rewritten_text[slug] = new_text

        # Track unresolved for the report (run on the ORIGINAL text so we see
        # what was unresolvable, not what we left alone).
        for line_match in RELATED_LINE_RE.finditer(text):
            rest = line_match.group(2).strip()
            if ALREADY_LINKED_RE.match(rest):
                continue
            status, _ = resolve_related(rest, aliases, slug)
            if status == SELF_REF:
                self_ref_by_page[slug].append(rest)
            elif status == NO_MATCH:
                no_match_by_page[slug].append(rest)

    # Pass 2: build reverse map (incoming) and inject backlinks.
    incoming: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for src_slug, targets in outgoing.items():
        src_title = pages[src_slug]["h1_title"]
        for tgt in targets:
            incoming[tgt].append((src_slug, src_title))

    # Compose final text per page (cross-refs rewritten + backlinks injected).
    final_text: dict[str, str] = {}
    for slug, new_text in rewritten_text.items():
        backlink_list = incoming.get(slug, [])
        section = render_backlinks_section(backlink_list)
        final_text[slug] = inject_backlinks(new_text, section)

    # Report
    if args.report:
        print(f"=== Cross-ref resolution report ({len(pages)} pages) ===")
        print(f"  resolved:       {counters['resolved']}")
        print(f"  already_linked: {counters['already_linked']}")
        print(f"  self_ref:       {counters['self_ref']}  (skipped — page references its own subject)")
        print(f"  no_match:       {counters['no_match']}  (no wiki page exists for this name)")
        print()
        print("=== No-match 'related:' names (could become future entity pages) ===")
        for slug in sorted(no_match_by_page.keys()):
            items = no_match_by_page[slug]
            if items:
                print(f"  {slug}:")
                for it in items:
                    print(f"    - {it}")
        print()
        print("=== Self-references suppressed (these names point at the same page) ===")
        for slug in sorted(self_ref_by_page.keys()):
            items = self_ref_by_page[slug]
            if items:
                print(f"  {slug}:")
                for it in items:
                    print(f"    - {it}")
        print()
        print("=== Backlink graph (incoming edges per page) ===")
        for slug in sorted(pages.keys()):
            inc = incoming.get(slug, [])
            print(f"  {slug}  <- [{', '.join(s for s, _ in inc) or '(none)'}]")

    # Write changes
    written = 0
    unchanged = 0
    diffs: list[str] = []
    for slug, info in pages.items():
        old = info["text"]
        new = final_text[slug]
        if old == new:
            unchanged += 1
            continue
        if args.dry_run:
            import difflib
            diff = "".join(difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{slug}.md",
                tofile=f"b/{slug}.md",
            ))
            diffs.append(diff)
        else:
            info["path"].write_text(new, encoding="utf-8")
            written += 1

    if args.dry_run:
        sys.stdout.write("".join(diffs))
        print(f"\n(dry-run) {len(diffs)} file(s) would change, {unchanged} unchanged",
              file=sys.stderr)
    else:
        print(f"Wrote {written} file(s), {unchanged} unchanged. "
              f"Resolved {counters['resolved']} new links, "
              f"already-linked {counters['already_linked']}, "
              f"self_ref {counters['self_ref']}, "
              f"no_match {counters['no_match']}.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
