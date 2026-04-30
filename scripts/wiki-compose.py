#!/usr/bin/env python3
"""
wiki-compose.py — write a complete memory/index.md from extractor outputs +
editorial sections preserved from the previous index.

Architecture:
  Deterministic sections (regenerated every run from TSVs):
    - Header / metadata block (Last regenerated, Coverage)
    - 📅 Daily logs (from dailies.tsv)
    - 🧠 Long-term memory (from memorymd-sections.txt)
    - 💬 Sessions — human-driven (from sessions-human.tsv)
    - 🤖 Sessions — automated (from summary.env AUTO_SECTION_PARAGRAPH)
    - Footer

  Editorial sections (preserved verbatim from the previous index.md):
    - 🪞 About this index (the wiki documents itself)
    - 🔖 Topic clusters

The LLM (or a human) curates the editorial sections; the script regenerates
everything else deterministically. This eliminates the regex-surgery-on-a-
giant-file failure mode and makes regens diff-perfect-idempotent (only
data-driven content changes between runs).

Usage:
  python3 wiki-compose.py
  python3 wiki-compose.py --out /tmp/wiki-build/index-draft.md   (preview, don't overwrite)
  python3 wiki-compose.py --check                                (verify a draft would equal input)
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace")))
OUT_DIR = Path(os.environ.get("WIKI_BUILD_DIR", "/tmp/wiki-build"))

INDEX_PATH = WORKSPACE / "memory" / "index.md"

# Section anchors used to extract editorial blocks from the existing index
SECTION_ABOUT = "## 🪞 About this index"
SECTION_DAILIES = "## 📅 Daily logs"
SECTION_MEMORYMD = "## 🧠 Long-term memory"
SECTION_HUMAN = "## 💬 Sessions — human-driven"
SECTION_AUTO = "## 🤖 Sessions — automated"
SECTION_TOPICS = "## 🔖 Topic clusters"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse dotenv-style file with single-quoted values. Tolerates embedded newlines
    via '\\n' or via balanced single quotes spanning lines."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8")
    # Match KEY='value' where value may contain anything except an unescaped single quote.
    # Single quotes inside are escaped as '\'' (close, escape, reopen) per the emit_env in shell.
    pattern = re.compile(r"^([A-Z_][A-Z0-9_]*)='((?:[^']|'\\''|\\')*)'\s*$", re.MULTILINE)
    for m in pattern.finditer(text):
        key, val = m.group(1), m.group(2)
        # Unescape: '\'' (literal) and \' both -> '
        val = val.replace("'\\''", "'").replace("\\'", "'")
        out[key] = val
    return out


def read_tsv(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return rows


def month_key(date_str: str) -> str:
    """Return YYYY-MM from a YYYY-MM-DD string."""
    return date_str[:7] if len(date_str) >= 7 else "????-??"


def month_label(yyyymm: str) -> str:
    try:
        d = datetime.strptime(yyyymm, "%Y-%m")
        return d.strftime("%B %Y")
    except ValueError:
        return yyyymm


def parse_existing_daily_bullets(prev_text: str | None) -> dict[str, str]:
    """Extract any pre-existing per-date bullet lines from the prior dailies section.

    Returns {YYYY-MM-DD: '<headline body>'} for every line matching either:
      - **YYYY-MM-DD** — <body>            (older, no daily-file link)
      - [**YYYY-MM-DD**](YYYY-MM-DD.md) — <body>   (current, daily-file linked)

    The returned headline body is just the post-em-dash content; the composer
    re-applies the date prefix and link wrapper consistently on output. This
    way LLM-polished headlines survive across regens without freezing in
    whatever link format the previous regen used.
    """
    out: dict[str, str] = {}
    if not prev_text:
        return out
    block = extract_editorial_block(prev_text, SECTION_DAILIES, SECTION_MEMORYMD)
    if not block:
        return out
    # Match either bare bold date or the linked form.
    pattern = re.compile(
        r"^- (?:\*\*(\d{4}-\d{2}-\d{2})\*\*|\[\*\*(\d{4}-\d{2}-\d{2})\*\*\]\([^)]+\)) — (.+)$",
        re.MULTILINE,
    )
    for m in pattern.finditer(block):
        date = m.group(1) or m.group(2)
        body = m.group(3)
        out[date] = body
    return out


def render_dailies(dailies: list[list[str]], prev_text: str | None = None) -> str:
    """Group dailies by month, newest first. Each entry: date + topic list joined with ' · '.

    For dates that already appear in the previous index, preserve the existing
    bullet line verbatim (LLM-polished headlines are kept). For new dates,
    render the raw H2 headers from the daily file.

    Each month header gets a first bullet linking to that month's session
    page (`sessions/YYYY-MM.md`) when it exists, so the workflow is:
    daily → month sessions → individual session.
    """
    existing_bullets = parse_existing_daily_bullets(prev_text)

    sessions_dir = WORKSPACE / "memory" / "sessions"
    months_with_pages: set[str] = set()
    if sessions_dir.exists():
        for p in sessions_dir.glob("*.md"):
            stem = p.stem  # YYYY-MM or UUID etc.
            if re.fullmatch(r"\d{4}-\d{2}", stem):
                months_with_pages.add(stem)

    # Discover which dates have a daily file on disk so we know whether to
    # render a clickable link for the date (only link if the file exists).
    memory_dir = WORKSPACE / "memory"
    dates_with_file: set[str] = set()
    if memory_dir.exists():
        for p in memory_dir.glob("*.md"):
            stem = p.stem
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
                dates_with_file.add(stem)

    def fmt_date_prefix(date: str) -> str:
        if date in dates_with_file:
            return f"[**{date}**]({date}.md)"
        return f"**{date}**"

    by_month: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in dailies:
        if len(row) < 2:
            continue
        date, topics_raw = row[0], row[1]
        # Always use the freshest link format for the date prefix; the
        # editorial body (after the em-dash) is what we preserve verbatim.
        if date in existing_bullets:
            body = existing_bullets[date]
        else:
            topics = [t for t in topics_raw.split("|") if t.strip()]
            if topics:
                body = " · ".join(topics)
            else:
                body = "*(empty)*"
        bullet = f"- {fmt_date_prefix(date)} — {body}"
        by_month[month_key(date)].append((date, bullet))

    lines = [
        "## 📅 Daily logs (`memory/YYYY-MM-DD.md`)",
        "",
        "_Editorial polish is preserved across regens; new entries land with raw H2 headers from the daily file._",
        "",
    ]
    for ym in sorted(by_month.keys(), reverse=True):
        lines.append(f"### {month_label(ym)}")
        if ym in months_with_pages:
            lines.append(f"- 📂 **Sessions:** [{month_label(ym)}](sessions/{ym}.md)")
        for date, bullet in sorted(by_month[ym], reverse=True):
            lines.append(bullet)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def slugify_anchor(heading: str) -> str:
    """Convert a markdown heading to a GitHub-flavored anchor slug.

    Lowercases, drops most punctuation, collapses whitespace to hyphens.
    Roughly matches GitHub's anchor algorithm: keep [a-z0-9-_], strip the
    rest, collapse runs of hyphens.
    """
    s = heading.lower()
    # Replace whitespace with hyphen
    s = re.sub(r"\s+", "-", s.strip())
    # Drop everything except alnum, hyphen, underscore
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    # Collapse multiple hyphens
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def render_memorymd(sections_path: Path) -> str:
    """Render the Long-term memory (MEMORY.md) section.

    Section title links to `../MEMORY.md` (one level up from `memory/`).
    Each H2/H3 entry becomes a link to the matching anchor on that file.
    """
    memory_md_link = "../MEMORY.md"
    if not sections_path.exists():
        body = "_(MEMORY.md not found)_"
    else:
        lines_raw = [ln.strip() for ln in sections_path.read_text().splitlines() if ln.strip()]
        bullets = []
        for ln in lines_raw:
            stripped = re.sub(r"^#+\s*", "", ln)
            indent = "  " if ln.startswith("###") else ""
            anchor = slugify_anchor(stripped)
            bullets.append(f"{indent}- [{stripped}]({memory_md_link}#{anchor})")
        body = "\n".join(bullets) if bullets else "_(no sections)_"
    return (
        f"## 🧠 Long-term memory ([`MEMORY.md`]({memory_md_link}))\n\n"
        "Curated, hand-edited durable memory. Referenced only in main sessions (privacy guard).\n\n"
        f"{body}\n"
    )


def render_sessions_human(human: list[list[str]]) -> str:
    """Render the sessions section as monthly links (not inline rows).

    Each month gets one bullet pointing to `sessions/YYYY-MM.md`, with the
    count of session-date rows and unique sessions for that month. The
    actual per-session list lives on the monthly page so this section
    stays small even as sessions accumulate.
    """
    by_month: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in human:
        if len(row) < 4:
            continue
        date, uuid, _bytes, msg = row[0], row[1], row[2], row[3]
        by_month[month_key(date)].append((date, uuid, msg))

    sessions_dir = WORKSPACE / "memory" / "sessions"
    months_with_pages: set[str] = set()
    if sessions_dir.exists():
        for p in sessions_dir.glob("*.md"):
            stem = p.stem
            if re.fullmatch(r"\d{4}-\d{2}", stem):
                months_with_pages.add(stem)

    lines = [
        "## 💬 Sessions — human-driven",
        "",
        "Sessions where the first user turn is a real human prompt (not a cron job, system event, or subagent context). Each monthly page lists every session-date row with drill-down links: `detail` (turn-by-turn LLM summary), `full` (prose transcript), `tools` (transcript with tool calls and results).",
        "",
    ]
    for ym in sorted(by_month.keys(), reverse=True):
        rows = by_month[ym]
        unique = len({u for _d, u, _m in rows})
        if ym in months_with_pages:
            lines.append(
                f"- [{month_label(ym)}](sessions/{ym}.md) — {len(rows)} "
                f"session-date row{'s' if len(rows) != 1 else ''} · {unique} unique session{'s' if unique != 1 else ''}"
            )
        else:
            lines.append(
                f"- **{month_label(ym)}** — {len(rows)} session-date rows · {unique} unique "
                f"_(monthly page not yet generated; run `wiki-month-pages.py`)_"
            )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_auto(auto_paragraph: str) -> str:
    return f"## 🤖 Sessions — automated (cron / system / heartbeats)\n\n{auto_paragraph}\n"


def render_subagent(subagent_paragraph: str) -> str:
    if not subagent_paragraph.strip():
        return ""
    return f"## 🧪 Sessions — subagent tasks\n\n{subagent_paragraph}\n"


def extract_editorial_block(prev_text: str, start_anchor: str, end_anchor: str | None) -> str | None:
    """Pull the block from start_anchor (inclusive of section heading line) up to the
    line BEFORE end_anchor (or to the end of file if end_anchor is None).

    Trailing `---` separators and trailing blank lines are stripped so the
    composer can re-add them deterministically without accumulating cruft.
    """
    lines = prev_text.splitlines()
    start_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith(start_anchor):
            start_idx = i
            break
    if start_idx is None:
        return None
    if end_anchor is None:
        end_idx = len(lines)
    else:
        end_idx = len(lines)
        for j in range(start_idx + 1, len(lines)):
            if lines[j].startswith(end_anchor):
                end_idx = j
                break
    block_lines = lines[start_idx:end_idx]
    # Trim trailing blank lines and trailing `---` separators (the composer re-adds them).
    while block_lines and (block_lines[-1].strip() == "" or block_lines[-1].strip() == "---"):
        block_lines.pop()
    if not block_lines:
        return None
    return "\n".join(block_lines) + "\n"


def default_about_section(coverage_line: str) -> str:
    return (
        f"{SECTION_ABOUT} (the wiki documents itself)\n\n"
        "`memory/index.md` is the first piece of a Karpathy-style LLM Wiki layered on top "
        "of OpenClaw's existing memory system. It's a script-generated catalog of one-line "
        "summaries pointing at:\n\n"
        "- **Daily logs** — `memory/YYYY-MM-DD.md` chronological journal entries\n"
        "- **Sessions** — `~/.openclaw/agents/main/sessions/<uuid>.jsonl` raw conversation transcripts\n"
        "- **Long-term memory** — `MEMORY.md` curated decisions and durable context\n\n"
        "The deterministic sections (daily logs, long-term memory, sessions, auto-section, "
        "footer) are regenerated by `scripts/wiki-compose.py` every run. The editorial "
        "sections (this About, and Topic clusters) are preserved from the previous "
        "index.md. The LLM only edits the editorial sections.\n\n"
        "**Conventions:**\n"
        "- One line per file: `Date · UUID/name · headline`\n"
        "- Headlines are derived from `## H2` headers in dailies, or the first user prompt for sessions\n"
        "- Sessions are classified into three buckets: human-driven, subagent (regen tasks), and auto/system\n"
        "- This file is rebuild-safe — re-running the generator should produce minimal diffs\n"
    )


def default_topics_section() -> str:
    return (
        f"{SECTION_TOPICS} (rough)\n\n"
        "This is the seed of an entity layer. Each cluster lists the daily files / sessions "
        "where the topic appears most prominently. As `memory/wiki/<topic>.md` pages get "
        "written, they replace the cluster line.\n\n"
        "_(no clusters yet — populate manually or via a wiki-maintainer LLM pass)_\n"
    )


def compose_index(env: dict[str, str], existing_index: str | None) -> str:
    coverage_line = env.get("COVERAGE_LINE", "_(no coverage line)_")
    last_regen_line = env.get("LAST_REGENERATED_LINE", "**Last regenerated:** unknown")
    auto_paragraph = env.get(
        "AUTO_SECTION_PARAGRAPH",
        "_(no auto section paragraph)_",
    )
    subagent_paragraph = env.get("SUBAGENT_SECTION_PARAGRAPH", "")
    footer_line = env.get("FOOTER_LINE", "*(no footer)*")

    # Pull editorial sections from the existing index, or fall back to defaults
    if existing_index:
        about = extract_editorial_block(existing_index, SECTION_ABOUT, SECTION_DAILIES) or default_about_section(coverage_line)
        topics = extract_editorial_block(existing_index, SECTION_TOPICS, None) or default_topics_section()
        # Strip the "---\n*Generated...*" footer if it ended up in the topics block
        topics_lines = topics.splitlines()
        for i in range(len(topics_lines) - 1, -1, -1):
            if topics_lines[i].strip() == "---":
                topics_lines = topics_lines[:i]
                break
        topics = "\n".join(topics_lines).rstrip() + "\n"
    else:
        about = default_about_section(coverage_line)
        topics = default_topics_section()

    # Read TSVs
    dailies = read_tsv(OUT_DIR / "dailies.tsv")
    human = read_tsv(OUT_DIR / "sessions-human.tsv")

    parts = [
        "# Memory Index",
        "",
        "> The wiki layer. Browse this file to navigate the memory system without loading every file. The vector index handles \"what does this *say*\"; this file handles \"what is *here*\".",
        "",
        last_regen_line,
        "",
        f"**Coverage:** {coverage_line}",
        "",
        "---",
        "",
        about.rstrip(),
        "",
        "---",
        "",
        render_dailies(dailies, existing_index).rstrip(),
        "",
        "---",
        "",
        render_memorymd(OUT_DIR / "memorymd-sections.txt").rstrip(),
        "",
        "---",
        "",
        render_sessions_human(human).rstrip(),
        "",
        "---",
        "",
        render_auto(auto_paragraph).rstrip(),
        "",
    ]
    subagent_section = render_subagent(subagent_paragraph)
    if subagent_section:
        parts.extend([
            "---",
            "",
            subagent_section.rstrip(),
            "",
        ])
    parts.extend([
        "---",
        "",
        topics.rstrip(),
        "",
        "---",
        "",
        footer_line,
        "",
    ])
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="Write to alternate path (default: workspace memory/index.md)")
    ap.add_argument("--check", action="store_true", help="Compare composed output to current index.md and exit 0 if equal, 1 if different")
    args = ap.parse_args()

    env = parse_env_file(OUT_DIR / "summary.env")
    if not env:
        print("ERROR: summary.env not found or empty. Run wiki-extract.sh first.", file=sys.stderr)
        return 2

    existing = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else None
    composed = compose_index(env, existing)

    if args.check:
        if existing is None:
            print("Index does not exist — would be created.")
            return 1
        if existing == composed:
            print("Index is up-to-date (composed output matches current index.md).")
            return 0
        # Show a small diff hint
        from difflib import unified_diff
        diff_lines = list(unified_diff(
            existing.splitlines(), composed.splitlines(),
            fromfile="current/index.md", tofile="composed (would-be) index.md",
            lineterm="", n=2,
        ))
        print(f"Index would change. {len(diff_lines)} unified-diff lines.")
        for ln in diff_lines[:40]:
            print(ln)
        if len(diff_lines) > 40:
            print(f"... ({len(diff_lines) - 40} more lines)")
        return 1

    out_path = Path(args.out) if args.out else INDEX_PATH
    out_path.write_text(composed, encoding="utf-8")
    print(f"Wrote {out_path} ({len(composed.splitlines())} lines, {len(composed)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
