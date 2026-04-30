#!/usr/bin/env python3
"""
wiki-implicit-links.py — Layer 2 of the Karpathy-wiki entity-linking work.

Layer 1 (`wiki-cross-refs.py`) resolves explicit `- related: <name>` bullets
in each page's `## Cross-references` section.

Layer 2 (this script) scans the SYNTHESIZED BODY TEXT of each
`memory/wiki/<slug>.md` page for unlinked mentions of known entity names and
turns the FIRST occurrence per (page, target) into a link to the target's
wiki page. Skips self-references, code, existing links, headings, and the
sections owned by Layer 1 (`## Cross-references`, `## Backlinks`, sources
footer).

Idempotent. Deterministic (no LLM).

What gets linked:
  - Entity slugs that look like a single token. `raimd` is a candidate;
    `chex-hardware` is not (multi-word slug, never appears in body prose
    that way).
  - Hand-picked PRIMARY_TOKENS (mirrors `wiki-cross-refs.py`): one-word
    tokens that should resolve to a specific page (`raims`, `chex`,
    `MDMsgWriter`, etc.).

What does NOT get linked (protected regions):
  - Existing markdown links `[…](…)` (and image links `![…](…)`)
  - Inline code spans `` `…` ``
  - Fenced code blocks ```…```
  - Headings (any `#` line)
  - Frontmatter quote lines (`> …`) and the bold meta line below H1
  - The `## Cross-references` section (Layer 1 owns it)
  - The `## Backlinks` section (Layer 1 generates it)
  - The sources footer (everything after the final `---` separator)
  - Self-references (a token that points back at the same page)

Case sensitivity heuristic:
  - Tokens with ANY uppercase letter (e.g. `MDMsgWriter`) match
    case-sensitively, preserving the original casing.
  - All-lowercase tokens (e.g. `raimd`, `chex`) match case-insensitively,
    preserving the original casing in the link text.

By default, only the FIRST occurrence on each page gets linked per target.
Use --all to link every occurrence (noisier, useful for review).

Usage:
  python3 wiki-implicit-links.py             # rewrite all wiki/<slug>.md
  python3 wiki-implicit-links.py --dry-run   # show diff per file, don't write
  python3 wiki-implicit-links.py --report    # print per-page link summary
  python3 wiki-implicit-links.py --all       # link every occurrence (not just first)
  python3 wiki-implicit-links.py --only chex # restrict pages by slug substring
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

WORKSPACE = Path(os.environ.get(
    "OPENCLAW_WORKSPACE",
    str(Path.home() / ".openclaw" / "workspace"),
))
WIKI_DIR = WORKSPACE / "memory" / "wiki"
INDEX_PATH = WORKSPACE / "memory" / "index.md"

SECTION_CROSS_REFS = "## Cross-references"
SECTION_BACKLINKS = "## Backlinks"


# ---------------------------------------------------------------------------
# Utilities (mirror wiki-cross-refs.py — kept duplicated by repo convention).
# ---------------------------------------------------------------------------


def slugify(s: str) -> str:
    """Normalize a name for fuzzy comparison: lowercase alnum + hyphens."""
    s = re.sub(r"\([^)]*\)", "", s)
    s = s.replace("`", "").replace("*", "").replace("_", "")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def discover_pages() -> dict[str, dict]:
    """Return {slug: {path, h1_title, text}} for every wiki/<slug>.md file."""
    out: dict[str, dict] = {}
    if not WIKI_DIR.exists():
        return out
    for p in sorted(WIKI_DIR.glob("*.md")):
        if p.name.startswith("."):
            continue
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^# (.+?)\s*$", text, re.MULTILINE)
        h1 = m.group(1).strip() if m else p.stem
        out[p.stem] = {"path": p, "h1_title": h1, "text": text}
    return out


def parse_index_cluster_names() -> dict[str, str]:
    """Map slug -> cluster name from index.md `🔖 Topic clusters` bullets."""
    out: dict[str, str] = {}
    if not INDEX_PATH.exists():
        return out
    text = INDEX_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^- \*\*\[((?:[^\]]|\\\])+)\]\(wiki/([^)]+)\.md\)\*\* \u2014",
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        out[m.group(2)] = m.group(1)
    return out


# ---------------------------------------------------------------------------
# Token candidate set
# ---------------------------------------------------------------------------


# Mirrors wiki-cross-refs.py PRIMARY_TOKENS. Layer 2 needs to know about all
# the same hand-picked one-word aliases so a body-text mention of `raims` or
# `MDMsgWriter` resolves to the right entity page.
#
# NOTE FOR FORKERS: this dict and the matching one in wiki-cross-refs.py are
# the primary places where the linker gets customized for a specific corpus.
# The entries below are the original author's project tokens (raimd, chex,
# dyna, ...). Replace them with your own one-word tokens and target slugs,
# and keep the two dicts in sync. (A future shared wiki_common.py will
# eliminate this duplication.) Empty dict is fine if your corpus has no
# one-word entity tokens.
PRIMARY_TOKENS: dict[str, str] = {
    "raims": "networking-multicast-rvd-raims",
    "rvd": "networking-multicast-rvd-raims",
    "raicache": "networking-multicast-rvd-raims",
    "frr": "networking-multicast-rvd-raims",
    "dnsmasq": "networking-multicast-rvd-raims",
    "bind": "networking-multicast-rvd-raims",
    "systemd-nspawn": "networking-multicast-rvd-raims",
    "raimd": "raimd",
    "MDMsgWriter": "raimd",
    "MDFieldIter": "raimd",
    "RvMsg": "raimd",
    "RwfMsgWriter": "raimd",
    "TibMsgWriter": "raimd",
    "TibSassMsgWriter": "raimd",
    "JsonMsgWriter": "raimd",
    "RwfFieldListWriter": "raimd",
    "chex": "chex-hardware",
    "dyna": "multi-machine-setup",
    "pumpkin": "multi-machine-setup",
    "frame": "multi-machine-setup",
    "kling": "opencat-the-movie-kling-ai",
    "qwen": "llm-model-infrastructure-and-inference",
    "aider": "llm-model-infrastructure-and-inference",
    "openclaw": "openclaw-source-build-contribution-workflow",
}


def is_single_token_slug(slug: str) -> bool:
    """A slug like `raimd` looks like a body-prose word; `chex-hardware`
    does not. Multi-word slugs are skipped — they almost never appear
    verbatim in body text and matching them would mostly be false positives.
    """
    return "-" not in slug and bool(re.fullmatch(r"[a-z0-9]+", slug))


def build_token_map(pages: dict[str, dict]) -> dict[str, str]:
    """Build {literal_token_to_match: target_slug}. Tokens are matched
    by literal text (with case sensitivity decided per-token). Each entry
    is a candidate for body-text linking.

    Sources:
      - PRIMARY_TOKENS (hand-picked one-word aliases)
      - Single-token page slugs (`raimd`)
    """
    tokens: dict[str, str] = {}

    # Start with hand-picked primaries (only those whose target page exists).
    for tok, slug in PRIMARY_TOKENS.items():
        if slug in pages:
            tokens.setdefault(tok, slug)

    # Single-token page slugs map to themselves.
    for slug in pages:
        if is_single_token_slug(slug):
            tokens.setdefault(slug, slug)

    return tokens


# ---------------------------------------------------------------------------
# Region detection
# ---------------------------------------------------------------------------


# Span = (start, end) half-open byte indices in the page text.
Span = tuple[int, int]


def find_owned_section_bounds(text: str) -> list[Span]:
    """Return spans for sections owned by Layer 1 / synthesizer footer that
    Layer 2 must not touch.

    Sections protected:
      - `## Cross-references` block (start of heading line through the
        line before the next `## ` heading or final `---`)
      - `## Backlinks` block (same rule)
      - Final-`---`-onward (sources footer)
    """
    spans: list[Span] = []
    lines = text.splitlines(keepends=True)
    line_starts: list[int] = []
    pos = 0
    for ln in lines:
        line_starts.append(pos)
        pos += len(ln)
    end_of_text = pos

    def section_span(heading: str) -> Span | None:
        for i, ln in enumerate(lines):
            if ln.startswith(heading):
                start_byte = line_starts[i]
                # Find next `## ` heading or final `---`.
                end_byte = end_of_text
                for j in range(i + 1, len(lines)):
                    raw = lines[j].rstrip("\n")
                    if raw.startswith("## ") or raw == "---":
                        end_byte = line_starts[j]
                        break
                return (start_byte, end_byte)
        return None

    for heading in (SECTION_CROSS_REFS, SECTION_BACKLINKS):
        sp = section_span(heading)
        if sp is not None:
            spans.append(sp)

    # Sources footer: everything from the LAST `---` onward.
    final_hr_line: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip("\n") == "---":
            final_hr_line = i
            break
    if final_hr_line is not None:
        spans.append((line_starts[final_hr_line], end_of_text))

    return spans


def find_protected_spans(text: str) -> list[Span]:
    """Return all spans we must not rewrite, including:
      - Fenced code blocks ``` ... ```
      - Inline code spans `...`
      - Markdown link/image targets (we protect the whole `[...](...)`
        construct — both label and url — so we don't double-link)
      - Heading lines (`#` starts a heading; protect entire line)
      - Frontmatter blockquote lines (`> ...`)
      - The bold meta line right below the H1 if it's the form
        `**Status:** ... · **First mention:** ... · **Last mention:** ...`
        (well, headings + blockquotes already cover most of that — we
        also protect any line that starts with `**` and contains ` · `.)
      - Owned sections (Cross-references / Backlinks / sources footer)
    """
    spans: list[Span] = []

    # Fenced code blocks (multi-line, non-greedy)
    for m in re.finditer(r"```.*?```", text, flags=re.DOTALL):
        spans.append((m.start(), m.end()))

    # Inline code (single backticks, no newlines inside)
    for m in re.finditer(r"`[^`\n]+`", text):
        spans.append((m.start(), m.end()))

    # Markdown links and images (entire construct)
    for m in re.finditer(r"!?\[[^\]\n]*\]\([^)\n]+\)", text):
        spans.append((m.start(), m.end()))

    # Whole-line protections: headings, blockquotes, bold meta lines.
    # Walk lines and add line-spans where matched.
    pos = 0
    for line in text.splitlines(keepends=True):
        line_start = pos
        line_end = pos + len(line)
        stripped = line.lstrip()
        protect_line = False
        if stripped.startswith("#"):
            protect_line = True
        elif stripped.startswith(">"):
            protect_line = True
        elif stripped.startswith("**") and "·" in line:
            protect_line = True
        if protect_line:
            spans.append((line_start, line_end))
        pos = line_end

    # Owned sections
    spans.extend(find_owned_section_bounds(text))

    # Merge overlapping spans
    spans.sort()
    merged: list[Span] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def in_protected(idx: int, protected: list[Span]) -> bool:
    """True if byte index `idx` is inside any protected span."""
    # Linear scan is fine — wiki pages are ~50-100 lines and we only call
    # this for actual token matches (a few dozen per page worst case).
    for s, e in protected:
        if idx >= e:
            continue
        if idx < s:
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# Body-text linker
# ---------------------------------------------------------------------------


def compile_token_pattern(token: str) -> re.Pattern[str]:
    r"""Compile a `\b<token>\b` regex with appropriate case sensitivity.

    - Mixed-case tokens (`MDMsgWriter`) -> case-sensitive.
    - All-lowercase tokens (`raimd`) -> case-insensitive.
    """
    flags = 0 if any(c.isupper() for c in token) else re.IGNORECASE
    # Use \b on both ends. re.escape handles special chars in tokens like
    # `systemd-nspawn` (the `-` is fine, but escape is defensive).
    return re.compile(rf"\b{re.escape(token)}\b", flags)


def existing_link_targets(text: str) -> set[str]:
    """Return the set of target slugs already linked from this page (any
    `[label](<slug>.md)` reference where the href is a bare wiki-page md
    file in the same directory). Used to keep the script idempotent: if a
    page already links to a target, Layer 2 won't add another implicit
    link to that target on a later run, even if its FIRST mention is now
    inside a link and a *different* mention would otherwise look
    unlinked.
    """
    out: set[str] = set()
    for m in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", text):
        href = m.group(1)
        if "/" in href or "#" in href:
            continue
        if href.endswith(".md"):
            out.add(href[:-3])
    return out


def link_implicit_in_page(slug: str, text: str,
                          tokens: dict[str, str],
                          link_all: bool,
                          stats: dict) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite body text of a single page. Returns (new_text, additions)
    where additions is a list of (matched_substring, target_slug) tuples
    representing newly-added links (for reporting).

    Algorithm:
      1. Compute protected spans once.
      2. Pre-scan existing markdown links to find which targets the page
         already links to (idempotence anchor).
      3. For each candidate token, find all matches in the text. Filter
         out matches inside protected spans and matches that point at the
         same page (self-reference). When not --all, skip the entire
         token if the page already links to its target, otherwise keep
         only the first surviving match.
      4. Replace surviving matches in REVERSE byte-order so earlier offsets
         don't shift while we patch later ones.
    """
    protected = find_protected_spans(text)
    already_linked = existing_link_targets(text)

    # Collect (match_start, match_end, matched_text, target_slug) entries.
    additions: list[tuple[int, int, str, str]] = []

    for token, target_slug in tokens.items():
        if target_slug == slug:
            stats["self_skipped"] += 1
            continue
        if not link_all and target_slug in already_linked:
            # Already at least one explicit/implicit link to this target.
            # Don't add more on this page.
            continue
        pat = compile_token_pattern(token)
        first_taken = False
        for m in pat.finditer(text):
            start, end = m.start(), m.end()
            if in_protected(start, protected):
                continue
            if not link_all and first_taken:
                continue
            additions.append((start, end, m.group(0), target_slug))
            first_taken = True

    # Sort additions by start position; if two tokens overlap at the same
    # offset, prefer the longer match (more specific).
    additions.sort(key=lambda t: (t[0], -(t[1] - t[0])))

    # Drop any addition that overlaps a previously-accepted one.
    accepted: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, matched, target in additions:
        if start < last_end:
            continue
        accepted.append((start, end, matched, target))
        last_end = end

    if not accepted:
        return text, []

    # Apply replacements in reverse byte-order.
    out = text
    for start, end, matched, target in reversed(accepted):
        replacement = f"[{matched}]({target}.md)"
        out = out[:start] + replacement + out[end:]

    stats["links_added"] += len(accepted)
    return out, [(matched, target) for (_s, _e, matched, target) in accepted]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print unified diff per page; don't write files")
    ap.add_argument("--report", action="store_true",
                    help="Print per-page link additions summary")
    ap.add_argument("--all", action="store_true",
                    help="Link every occurrence per (page, target), not just first")
    ap.add_argument("--only", default=None,
                    help="Restrict pages by slug substring match")
    args = ap.parse_args()

    pages = discover_pages()
    if not pages:
        print(f"No wiki pages found in {WIKI_DIR}", file=sys.stderr)
        return

    tokens = build_token_map(pages)
    if not tokens:
        print("No linkable tokens (alias map is empty?)", file=sys.stderr)
        return

    if args.only:
        pages = {s: info for s, info in pages.items() if args.only in s}
        if not pages:
            print(f"No pages matched --only {args.only!r}", file=sys.stderr)
            return

    stats = {"self_skipped": 0, "links_added": 0}
    additions_by_page: dict[str, list[tuple[str, str]]] = {}
    new_text_by_page: dict[str, str] = {}

    for slug, info in pages.items():
        new_text, adds = link_implicit_in_page(
            slug, info["text"], tokens, args.all, stats,
        )
        new_text_by_page[slug] = new_text
        if adds:
            additions_by_page[slug] = adds

    if args.report:
        print(f"=== Implicit cross-link additions "
              f"({len(additions_by_page)} of {len(pages)} pages) ===")
        for slug in sorted(pages.keys()):
            adds = additions_by_page.get(slug, [])
            if not adds:
                print(f"  {slug}: (none)")
                continue
            grouped: dict[str, list[str]] = defaultdict(list)
            for matched, target in adds:
                grouped[target].append(matched)
            print(f"  {slug}:")
            for target in sorted(grouped):
                samples = grouped[target]
                print(f"    -> {target}.md: {len(samples)} link(s) ({', '.join(samples[:3])}"
                      f"{'…' if len(samples) > 3 else ''})")
        print(f"\nTotal links added: {stats['links_added']}")

    written = 0
    unchanged = 0
    diffs: list[str] = []
    for slug, info in pages.items():
        old = info["text"]
        new = new_text_by_page[slug]
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
        print(f"\n(dry-run) {len(diffs)} file(s) would change, "
              f"{unchanged} unchanged, +{stats['links_added']} link(s)",
              file=sys.stderr)
    else:
        print(f"Wrote {written} file(s), {unchanged} unchanged, "
              f"+{stats['links_added']} link(s) added.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
