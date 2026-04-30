# An LLM-Synthesized Personal Wiki

*An architecture white paper for a self-maintaining knowledge layer over an
agent's working memory. Pipeline, data flow, LLM usage, algorithms, and how
it relates to Andrej Karpathy's "LLM wiki" sketch.*

**Status:** working system, ~4,000 lines of glue code, in production over 12
weeks of session transcripts and daily notes.

**Audience:** technically-minded readers comfortable with shell, Python,
markdown, and the basic shape of an LLM API.

**License:** intended for a public GitHub repository so others can iterate.

---

## 1. Introduction

### What this is for

This is a shared continuity layer for two parties with complementary
memory limits.

**The agent has zero session-to-session memory.** Every fresh
conversation starts with the assistant knowing nothing about the user's
codebases, the decisions made last week, the bug that was tracked down
last month, or the conventions established three months ago. The
agent's amnesia is total and resets on every cold start.

**The human has gradient memory loss.** The author of this system
maintains a stack of long-running projects — a market-data library,
a pub/sub messaging system, a multicast-aware caching tier,
contributions to an agent runtime, plus a dozen smaller side
things. At any given moment one or two are active and the rest
are dormant. When a dormant project comes back — "what state is
raimd in? what was the last thing I tried with the virtual-dispatch
refactor?" — the human can no more recall the specifics than the
agent can.

The wiki addresses both at once. When the human comes back to a
dormant project, they read the entity page (`memory/wiki/raimd.md`)
to reconstruct context. When the agent starts a fresh session and
needs context to be useful, it reads the same entity page. **Same
artifact, two readers, both refreshed by the same pipeline.**

That shared-substrate property is the design's distinguishing feature.
Competitor systems treat the wiki as a thing the LLM produces for the
LLM (the cache that lets the agent skip RAG) or as a thing the human
produces for the human (a Zettelkasten with an LLM helper). This
system treats it as the *single* view of project state that both
parties draw from. The fact that an LLM does the synthesis is an
implementation detail — the artifact would be useful to the human
even if no agent ever read it.

It is not a research tool. It does not ingest papers. It has no
opinion about the SOTA. It is what you get when an engineer notices
that both they and their agent are failing to retain context across
long-running work, and builds the smallest deterministic layer that
fixes both at once.

### Where it sits in the broader memory-systems landscape

Personal "second-brain" / "memory" systems for large language models
tend to land in one of three places:

1. **Vector retrieval over raw notes.** Embed every chunk, do nearest-
   neighbour lookup at query time, paste hits into context. Works at any
   scale. Returns fragments, not understanding.
2. **Hand-curated wiki / Zettelkasten.** Human writes pages. The LLM is just
   a reader. Beautiful structure, terrible scaling: no human writes 200
   well-cross-linked pages a year on top of a day job.
3. **LLM-synthesized wiki**, the pattern Andrej Karpathy sketched
   publicly and which a wave of public implementations is now
   exploring. The LLM compiles raw sources into an interlinked
   markdown wiki once and maintains it incrementally; queries hit the
   compiled artifact, not the raw chunks.

This paper describes a system in family (3), but with a different
optimization target than the other public implementations. They are
largely aimed at AI researchers organizing papers, agentic research
workflows, or general document-knowledge-base tooling. This system
is aimed at one specific user: a senior engineer with shipped code
in production whose agent needs to remember the last six months of
work.

The shape of the system follows from that target:

- **Conversation transcripts and daily journal notes are the primary
  input**, not documents dropped into a `sources/` directory.
  An engineer's working knowledge lives in the conversations where
  decisions got made, not in a folder of papers waiting to be read.
- **Cross-references and backlinks are resolved by deterministic
  post-passes**, not by the LLM, with idempotence enforced and
  verified end-to-end. This thing has to run silently in the
  background, possibly on a cron, and not introduce drift.
- **Vector retrieval is kept** as an orthogonal channel. People
  remember by topic *and* by time *and* by partial recall; an
  engineer debugging "didn't I see this exact stack trace four
  months ago?" needs all three.
- **The LLM is treated as the prose synthesizer of last resort.**
  Not because LLMs are bad, but because every byte the LLM writes is
  a byte that has to be cached, invalidated, and trusted, and the
  user already knows where deterministic code is the better tool.

The system was built by one person on top of an agent runtime called
OpenClaw. Everything is plain markdown on disk. Nothing in the design
requires OpenClaw specifically — the input layer is just "directory of
JSONL conversation transcripts plus directory of daily notes," which any
agent runtime can produce.

### What it produces

For a single user with about three months of activity, the current run
yields:

- **32 polished daily-log pages** — one per day the human worked.
- **138 per-session pages** — three views (summary / full prose / full
  transcript including tool calls) of each of the 46 human-driven
  sessions in the corpus.
- **4 per-month rollup pages** — every session that month, grouped.
- **1 master `index.md`** — a hand-readable, link-rich catalog of
  everything: daily logs, monthly session indices, long-term memory
  sections, and topic clusters.
- **11 entity pages** — Karpathy-style synthesized articles about the
  recurring projects, hardware, and concepts that show up across the
  raw sources.
- **A backlink graph** — every entity page lists which other entity
  pages link to it.
- **Auto-cross-linked prose** — a mention of `raimd` in any entity
  page's body text becomes a clickable link to `raimd.md` on its first
  occurrence.

Re-running the entire pipeline takes a few seconds in steady state
(everything is content-addressable cached) and costs about USD $0.05–$0.10
in LLM calls when caches are cold.

### Two-sentence summary

A two-tier markdown wiki, generated and re-generated from conversation
transcripts plus daily notes, where one tier is deterministic and the
other is LLM-synthesized with content-hash caching. The deterministic
tier is where idempotence and trust live; the LLM tier is where
synthesis lives; cross-references between them are also deterministic
and idempotent.

---

## 2. Layered architecture

```
                       ┌──────────────────────────────────────────────┐
                       │  Tier 0 — RAW SOURCES (immutable inputs)     │
                       │                                              │
                       │  • sessions/<uuid>.jsonl                     │
                       │    (one JSONL line per agent turn,           │
                       │     full content including tool calls)       │
                       │                                              │
                       │  • memory/YYYY-MM-DD.md                      │
                       │    (human-authored daily journal)            │
                       │                                              │
                       │  • MEMORY.md                                 │
                       │    (human-curated long-term notes)           │
                       └──────────────────────────────────────────────┘
                                          │
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Tier 1 — DETERMINISTIC EXTRACTS (cheap, regenerable, no LLM)        │
   │                                                                      │
   │  /tmp/wiki-build/                                                    │
   │   ├ dailies.tsv              date  ↦  H2 headlines                   │
   │   ├ sessions-human.tsv       date  uuid  bytes  first-user-message   │
   │   ├ sessions-subagent.tsv    same shape, different bucket            │
   │   ├ sessions-auto.tsv        date  uuid  bytes  (no message)         │
   │   ├ memorymd-sections.txt    long-term MEMORY.md ## section list     │
   │   └ turns/<uuid>.jsonl       per-turn (user_text, assistant_text)    │
   │                                                                      │
   │  Cleared and regenerated on every run.                               │
   └─────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Tier 2 — LLM-SYNTHESIZED PAGES (cached by content hash)             │
   │                                                                      │
   │  • Per-turn one-line summaries                                       │
   │       ~/.../memory/sessions/.summaries.json                          │
   │       Cache key:  <session-uuid>:<assistant-message-id>              │
   │                                                                      │
   │  • Per-entity wiki pages                                             │
   │       memory/wiki/<slug>.md                                          │
   │       Cache key:  sha256 of concatenated normalized source texts     │
   │                   stored in memory/wiki/.entity-cache.json           │
   │                                                                      │
   │  Re-runs are free when source content is unchanged (cache hits ≈100%)│
   └─────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Tier 3 — DETERMINISTIC RENDER + LINK PASSES (idempotent)            │
   │                                                                      │
   │  • Per-session pages       memory/sessions/<uuid>.md                 │
   │  • Full-prose companions   memory/sessions/<uuid>.full.md            │
   │  • Tools companions        memory/sessions/<uuid>.tools.md           │
   │  • Per-month indexes       memory/sessions/YYYY-MM.md                │
   │  • Master index            memory/index.md                           │
   │                                                                      │
   │  Then:                                                               │
   │  • wiki-cross-refs.py    Layer 1 entity-linker + backlinks           │
   │  • wiki-implicit-links.py Layer 2 body-text linker                   │
   │  • wiki-link-topics.py    Date / UUID / MEMORY.md auto-linker        │
   │                                                                      │
   │  Every step in this tier is a pure function of its inputs.           │
   │  Running the tier twice produces byte-identical files.               │
   └─────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Tier 4 — VECTOR INDEX (orthogonal retrieval channel)                │
   │                                                                      │
   │  Local fine-tuned bge-small embedding model, served on               │
   │  http://127.0.0.1:8787/v1, indexes:                                  │
   │   • memory/*.md (daily logs, MEMORY.md, all wiki pages)              │
   │   • sessions/*.jsonl (chunked transcript content)                    │
   │                                                                      │
   │  Used for "what does the corpus say about X?" — answers WITH the     │
   │  wiki, not instead of it. The wiki answers "what is here?" and       │
   │  the vector index answers "what is said?".                           │
   └─────────────────────────────────────────────────────────────────────┘
```

The design enforces a property each tier has alone: **only Tier 2 is
non-deterministic.** Tier 0 is human input; Tiers 1, 3, and 4 are pure
functions of their inputs. The LLM's contribution is bounded, cached,
and surrounded on both sides by deterministic code.

---

## 3. The pipeline, in eleven steps

Each step is one script under `scripts/`. The numbering matches the
maintainer skill (`skills/wiki-maintainer/SKILL.md`).

| # | Script | What it does | LLM? | Cache? |
|---|---|---|---|---|
| 1 | `wiki-extract.sh` | Build TSVs of dailies + classify sessions (human / subagent / auto). Handles modern + early-format sessions, hybrid sessions, multi-day continuations. | no | no |
| 2 | `wiki-turns-extract.py` | Pull per-turn `(user, assistant)` pairs from each human session into per-session JSONL. Strips envelope metadata and skips system events. | no | no |
| 3 | `wiki-turns-summarize.py` | Async parallel calls to a small fast model (Claude Haiku). One sentence per turn. **Persistent cache** keyed by `<uuid>:<msg_id>`. | yes | yes |
| 4 | `wiki-sessions-compose.py` | Render `<uuid>.md` per-session summary pages, plus shell out to `wiki-session-reconstruct.py` for `.full.md` and `.tools.md` companion files. | no | no |
| 5 | `wiki-session-reconstruct.py` | Build full prose / tools / raw transcripts. Largest-variant-wins logic across `.jsonl`, `.reset.*`, `.deleted.*`, `.checkpoint.*`. | no | no |
| 6 | `wiki-month-pages.py` | Generate `sessions/YYYY-MM.md` with all sessions for that month. Cross-month sessions appear in both. | no | no |
| 7 | `wiki-compose.py` | Write `memory/index.md` from extracted data + previous editorial sections (preserved verbatim across regens). | no | no |
| 8 | `wiki-entity-pages.py` | Synthesize `memory/wiki/<slug>.md` per topic cluster. **Cached by source content-hash.** | yes | yes |
| 9 | `wiki-cross-refs.py` | Layer 1: resolve `- related: <name>` lines in entity pages to wiki-page links and inject auto-generated `## Backlinks` sections. | no | no |
| 10 | `wiki-implicit-links.py` | Layer 2: scan synthesized body text for unlinked mentions of known entity names and link the first occurrence per (page, target). | no | no |
| 11 | `wiki-link-topics.py` | Add clickable links to dates / session UUIDs / `MEMORY.md` refs / wiki entity pages in the editorial sections of `index.md`. | no | no |

Two of the eleven steps call an LLM. **Both are cached.** A re-run with no
new input touches no API, writes no files, and finishes in a couple of
seconds.

### Quick path

```bash
. ~/.openclaw/env.sh                        # loads ANTHROPIC_API_KEY
cd ~/.openclaw/workspace

bash    scripts/wiki-extract.sh             # 1
python3 scripts/wiki-turns-extract.py       # 2
python3 scripts/wiki-turns-summarize.py     # 3   LLM, cached
python3 scripts/wiki-sessions-compose.py    # 4
python3 scripts/wiki-month-pages.py         # 6
python3 scripts/wiki-compose.py             # 7
python3 scripts/wiki-entity-pages.py        # 8   LLM, cached
python3 scripts/wiki-cross-refs.py          # 9
python3 scripts/wiki-implicit-links.py      # 10
python3 scripts/wiki-link-topics.py         # 11
openclaw memory index                       # refresh vector index
```

(Step 5 is invoked by step 4 per session; not run standalone.)

---

## 4. Data flow, end-to-end

The system is best understood as a sequence of value transformations
applied to the same underlying corpus, with each transformation persisted
to disk so later steps can be rerun cheaply.

```
   sessions/<uuid>.jsonl                  ─────►  Tier 1 TSVs + per-turn JSONL
   memory/YYYY-MM-DD.md                            (deterministic extract)
   MEMORY.md
                                                          │
                                                          ▼
                                        ┌──────────────────────────┐
                                        │ LLM #1: per-turn summary │
                                        │  Haiku, ≤22 words/turn   │
                                        │  cache: uuid:msg_id      │
                                        └──────────────────────────┘
                                                          │
                                                          ▼
                                  per-session .md + .full.md + .tools.md
                                  per-month YYYY-MM.md
                                  master index.md   (human-readable catalog)
                                                          │
                                                          ▼
                              "Topic clusters" block in index.md is the
                              human/LLM-curated list of which sources
                              feed which entity. The LLM proposes; the
                              human edits; the script regenerates the
                              rest of index.md around it.
                                                          │
                                                          ▼
                                        ┌──────────────────────────┐
                                        │ LLM #2: entity synthesis │
                                        │  Haiku, ~one page each   │
                                        │  cache: source hash      │
                                        └──────────────────────────┘
                                                          │
                                                          ▼
                                       memory/wiki/<slug>.md  (entity pages)
                                                          │
                                                          ▼
                                Layer 1: explicit cross-refs + backlinks
                                Layer 2: implicit body-text links
                                Layer 3: dates / UUIDs / MEMORY.md anchors
                                                          │
                                                          ▼
                                A fully-linked, browsable, vector-indexable
                                personal wiki. Every regen converges.
```

The most important property of the data flow is that **each tier has a
clear contract for what it produces and what it preserves**. The
composer for `index.md`, for example, regenerates the deterministic
data sections every run but treats the editorial blocks (`🪞 About this
index`, `🔖 Topic clusters`) as opaque preserved-verbatim content. So a
human can edit those blocks freely and the next composer run won't trample
the edits — yet the rest of the file remains a pure function of the TSVs.

---

## 5. LLM usage in detail

The system uses an LLM in exactly two places. Both call Claude Haiku
(currently `claude-haiku-4-5`) through the Anthropic Messages API. Both
are persistent-cached on disk. Both are async-parallel.

### 5.1 Per-turn summarization (step 3)

**Input:** one user request and the assistant's response, with metadata
envelopes stripped and assistant text truncated to about 4000 characters
to keep prompts cheap.

**System prompt:**

> You write tight one-line summaries of an assistant's response to a user
> request. Goal: future-self at a glance — what did the assistant DO or
> CONCLUDE? Constraints: ≤ 22 words, no emoji, no markdown, no leading
> verb required. Drop hedge words. Output ONLY the summary line, no
> quotes, no prefix.

**Output:** one sentence per turn, written to a JSONL file plus a
persistent JSON cache.

**Cache key:** `<session-uuid>:<assistant-message-id>`. The assistant
message ID is stable across re-extractions (it's part of the runtime's
on-disk transcript format), so the cache hits 100% of unchanged turns.

**Volume:** roughly 800 turns across 90 sessions in the current corpus.
First run: ~1–3 minutes wall-clock at concurrency 8. Subsequent runs:
~1 second.

**Cost:** Haiku rates, ~120K input + ~13K output tokens per cold full
run, total ≈ USD $0.05 to summarize the entire corpus from scratch.

### 5.2 Per-entity synthesis (step 8)

**Input:** the cluster definition from `index.md`'s `🔖 Topic clusters`
block (entity name + a list of source files), plus the full text of all
listed sources concatenated.

**System prompt** (excerpt — full prompt is ~40 lines in
`wiki-entity-pages.py`):

> You are a wiki maintainer synthesizing a Karpathy-style entity page from
> raw source notes.
>
> Goal: produce a focused, present-tense reference page about ONE entity
> (a project, concept, system, person, or hardware item). Future-self
> should be able to read this page and answer "what is this thing, what's
> been done with it, and what's next" without re-reading the sources.
>
> Format requirements (strict):
> - H1 title: the entity name
> - A blockquote single-line elevator pitch (≤25 words)
> - A status line: `**Status:** ... · **First mention:** YYYY-MM-DD · **Last mention:** YYYY-MM-DD`
> - `## What it is` — 2-4 paragraphs of synthesis, plain prose, no bullets
> - `## Key events / decisions` — bulleted, chronological, each `**YYYY-MM-DD** —`
> - `## Open questions / next steps` — only if sources contain unresolved threads
> - `## Cross-references` — list of `- related: <name>` lines (plain text — link script handles them)
>
> Style: declarative, no hedging, third-person about the work, no
> emoji, no quoting verbatim. Synthesize.

**Output:** one markdown file per entity, dropped into `memory/wiki/`
followed by an auto-generated footer that lists the sources and a
content-hash watermark.

**Cache key:** sha256 of the concatenated normalized source texts.
Stored in `memory/wiki/.entity-cache.json`. If the hash hasn't changed
since the last synthesis, the page is left alone byte-for-byte.

**Volume:** currently 11 entities, average 4–7 sources each. First run:
~1 minute at concurrency 4. Re-runs: ~1 second.

**Cost:** ~120K input + ~13K output tokens for a full re-synthesis ≈
USD $0.05–$0.10. Effectively zero in steady state.

### 5.3 Why only here?

The deliberate constraint: **only let the LLM write where it is solving
a problem deterministic code cannot.** Specifically:

- "Summarize this conversation in one sentence" — no rules-based system
  is going to beat a small language model on this.
- "Synthesize an article-length page from a stack of conversational
  source notes" — same.

Everything else (linking related-name strings to slugs, building backlink
graphs, scanning prose for known entity names, parsing transcript JSONL,
deduplicating across `.deleted.*` / `.reset.*` variants, composing the
master index) is regex, set arithmetic, or graph traversal. Doing those
in Python is faster, idempotent, free, and inspectable. Doing them in an
LLM would be slower, non-deterministic, expensive, and fragile.

---

## 6. Algorithms

The interesting engineering is in Tier 3, where idempotence has to hold
under repeated runs that may include LLM-introduced variation. Three
algorithms in particular are worth describing.

### 6.1 Editorial preservation in `wiki-compose.py`

**Problem:** `index.md` has both deterministic content (lists of dailies,
sessions, MEMORY.md sections) and human-curated editorial content
("About this index" prose, "Topic clusters" definitions). The composer
must regenerate the former from TSVs every run, but never trample the
latter.

**Solution:** treat the file as a sequence of named sections delimited by
H2 anchors. The composer reads the previous file, extracts the editorial
sections verbatim, builds the deterministic sections from extracts, and
splices everything back together with a canonical section order. Re-runs
are byte-stable because the deterministic sections are pure functions
of the TSVs and the editorial sections are pass-through.

This avoids the regex-surgery-on-a-giant-file failure mode and makes
every regen diff-perfect.

### 6.2 Layer 1 + Layer 2 entity-linker

**Problem (Layer 1):** entity pages end with a `## Cross-references`
section listing `- related: <name>` lines (LLM output). Names are
plain text. We want them as clickable links to other entity pages,
plus a reverse `## Backlinks` section on every page that has incoming
links.

**Algorithm:**

1. Build an alias map keyed by `slugify(name)` and pointing to a
   target slug. Populate from:
   - Each page's slug itself (`raimd` → `raimd`)
   - Each page's H1 title
   - The cluster name from `index.md`
   - A small hand-picked dictionary of one-word "primary subject"
     tokens (e.g. `MDMsgWriter` → `raimd`, `chex` → `chex-hardware`).
2. Walk every page's `## Cross-references` section. For each
   `- related: <name>` line, slugify and look up the alias map.
   Resolved → rewrite as `- related: [<name>](<slug>.md)`.
   Self-reference (target == this page) → leave alone.
   No match → leave alone.
3. Build the reverse map (incoming edges per slug) from the
   resolved links. For each page that has incoming links, render
   a `## Backlinks` section in canonical form and inject it at a
   stable position (right before the `---` sources footer).
4. Idempotence: the section text is canonical and ordered, the
   stripper deletes the old section before injecting the new one,
   and already-resolved bullets are detected and counted toward
   target sets without being rewritten again.

**Problem (Layer 2):** LLM-synthesized prose mentions entity names
constantly without ever marking them as links. We want the first
mention of `raimd` in any page's body text to become a link to
`raimd.md`.

**Algorithm:**

1. Build a candidate token map:
   - Single-token slugs (`raimd` ✓, `chex-hardware` ✗ — multi-word
     slugs almost never appear verbatim in body prose).
   - The same hand-picked PRIMARY_TOKENS dict as Layer 1.
2. Compute a "protected spans" set over the page text covering:
   existing markdown links and images, inline code spans, fenced
   code blocks, heading lines, blockquote lines, the bold meta line
   below H1, and the regions Layer 1 owns (`## Cross-references`,
   `## Backlinks`, sources footer).
3. Pre-scan the page for existing markdown links of the form
   `[label](<slug>.md)` and record which target slugs are already
   linked from this page. (This is the **idempotence anchor** —
   without it, the algorithm is non-idempotent.)
4. For each candidate token whose target is not already linked from
   this page and not the page itself, find all `\b<token>\b`
   matches. Lowercase tokens match case-insensitive (so `Pumpkin`
   at sentence start still links); mixed-case tokens
   (`MDMsgWriter`) match case-sensitive. The first match outside
   the protected spans is accepted.
5. Apply replacements in reverse byte-order so earlier offsets
   don't shift while later ones are patched. Overlapping accepted
   matches resolve in favour of the longer (more specific) match.

**Idempotence proof sketch:** after the script runs once, every
target reachable from this page is linked exactly once. On the next
run, step 3 finds the existing link and step 4 skips the token
entirely. Output is byte-identical. The naïve version of step 4
(skip protected matches, take first surviving) is *almost* idempotent
but actually isn't: once the original first match is wrapped in a
link (= protected), the next run links the *next* mention, and the
next, and so on until every mention is linked. The pre-scan in step
3 is what makes the algorithm a true fixpoint.

### 6.3 Largest-variant-wins canonical session pick

**Problem:** the runtime occasionally archives a session by renaming it
to `.deleted.<timestamp>`, `.reset.<timestamp>`, or `.checkpoint.<timestamp>`.
A given session UUID may have multiple files on disk:
`<uuid>.jsonl`, `<uuid>.reset.20260417180000`, etc. They overlap but
aren't identical (a `.reset` archive is a snapshot at the time of reset;
the live `.jsonl` continues from there). For wiki purposes we want the
single most-complete view.

**Solution:** for each UUID, glob all variants and pick the one with the
largest byte size. This is correct because the runtime's archive
operations are append-only-then-rename, so a longer file is strictly a
superset of a shorter one. The picker is one function (`canonical_path`)
shared by every consumer in the pipeline.

This is also what made the system robust to a runtime bug in early 2026
where some sessions were unlinked outright instead of renamed: the
daily-log layer survived as the durable record, and per-session pages
that had already been generated stayed in place.

---

## 7. Storage shape on disk

```
~/.openclaw/workspace/
├── MEMORY.md                              # human-curated long-term notes
├── memory/
│   ├── index.md                           # master catalog (Tier 3)
│   ├── 2026-04-30.md                      # daily logs (Tier 0, human-authored)
│   ├── 2026-04-29.md
│   ├── ...
│   ├── sessions/
│   │   ├── <uuid>.md                      # per-session summary  (Tier 3)
│   │   ├── <uuid>.full.md                 # full prose transcript (Tier 3)
│   │   ├── <uuid>.tools.md                # transcript with tool calls (Tier 3)
│   │   ├── 2026-04.md                     # per-month rollup (Tier 3)
│   │   └── .summaries.json                # per-turn summary cache (LLM #1)
│   └── wiki/
│       ├── raimd.md                       # entity pages (Tier 2 LLM #2)
│       ├── chex-hardware.md
│       ├── ...
│       └── .entity-cache.json             # entity content-hash cache (LLM #2)
└── scripts/
    └── wiki-*.py / wiki-*.sh              # the eleven pipeline steps
```

Everything checked into git is plain markdown plus the JSON caches. There
is no database, no proprietary blob, no service the user can't read with
`cat`. Caches can be deleted at any time; the next run regenerates them.

---

## 8. Performance, cost, scale

Measured on a single workstation (chex, Ryzen 9 7950X, 96 GB RAM), with
32 daily files and 46 human-driven session transcripts as of late
April 2026:

| Phase | Cold (no caches) | Warm (caches valid) |
|---|---|---|
| Tier 1 extracts (steps 1–2)    | ~3 s   | ~3 s   |
| Per-turn summarization (3)     | 1–3 min at concurrency 8 | <1 s |
| Per-session render (4–6)       | ~5 s   | ~5 s   |
| Master index compose (7)       | ~1 s   | ~1 s   |
| Per-entity synthesis (8)       | ~60 s at concurrency 4 | <1 s |
| Cross-refs + linkers (9–11)    | ~1 s   | ~1 s   |
| Vector index refresh           | ~10 s  | ~10 s  |
| **Total**                      | **~3–5 min** | **~20 s** |

LLM cost cold: ≈ USD $0.10. LLM cost warm: $0.

Scaling characteristics:

- **Tier 1** is O(N) in input size; each step is a single linear pass
  over JSONL plus a few `awk`/`grep` filters.
- **Per-turn summarization** is O(turns) but cache-bounded — a stable
  conversation history makes it effectively O(new turns).
- **Per-entity synthesis** is O(entities × source size); cap each
  cluster at 4–7 sources and the per-call prompt stays tiny.
- **Cross-ref + linker passes** are O(pages × candidate tokens). At ~10
  pages and ~30 tokens, every pass is sub-second.

The architectural cost question is "what happens at 1,000 entity pages?"
The honest answer: the LLM-touching steps stay cheap (cache, content
hash) but the linkers' alias map gets unwieldy. The fix is a modest
refactor — keep aliases in a YAML file, build the regex set once per
run instead of per-token, possibly precompute a global token-position
index. None of that is needed at current scale.

---

## 9. Failure modes and recoveries

The system has been hit by every one of these in the wild and has a
named recovery for each:

| Failure | Recovery |
|---|---|
| LLM rate limit / API error during step 3 or 8 | Cache is written incrementally; rerun resumes where it stopped. |
| LLM returns slightly different prose for the same source on a re-synthesis | Content-hash cache prevents re-synthesis when source is unchanged. The hash is over the **inputs**, not the output, specifically so output drift doesn't invalidate the cache. |
| Operator hand-edits a synthesized entity page | The page's body is overwritten on the next cold synthesis; the editorial place to make permanent edits is the source files (daily logs, MEMORY.md), not the entity page. |
| Operator hand-edits the master index editorial sections | Preserved verbatim across regens (see §6.1). |
| Runtime archives a session as `.deleted.<ts>` | Largest-variant-wins picks up the archive automatically (see §6.3). |
| Runtime *unlinks* a session outright | Daily log + previously-generated per-session pages survive as the durable record. |
| Layer 2 implicit-linker linked the wrong word | Add an entry to PRIMARY_TOKENS or rename the page slug; rerun is idempotent. |
| Cluster definition in `index.md` is wrong | Edit the `🔖 Topic clusters` block; next synthesis run picks up the new source list and re-syntheses just the affected entities (others stay cached). |

The general pattern: **the operator's interventions go into the editorial
inputs, the system's outputs are regenerable from there, and caches
shield against doing expensive work twice.**

---

## 10. Vector retrieval, alongside, not instead

Tier 4 is a small fine-tuned embedding model (`bge-small-en-v1.5`,
33M parameters, 384 dimensions, fine-tuned on synthetic query/doc pairs
generated from this user's own corpus). It runs on CPU on the same host,
exposes an OpenAI-compatible `/v1/embeddings` endpoint on port 8787,
and indexes both the markdown files (`memory/*.md`, `MEMORY.md`,
`memory/wiki/*.md`) and the session transcripts.

The two retrieval channels answer different questions:

| Question | Wiki | Vector |
|---|---|---|
| What is `raimd`? | One-page synthesized answer | Twelve transcript fragments mentioning it |
| When did the chex MCE happen? | `chex-hardware.md` lead paragraph | Three matching daily-log chunks |
| Did I ever try X? | Probably not — entities are recurring topics | Yes — fragments surface even one-shot mentions |
| What did I do last Tuesday? | Daily logs, linked from index | Useless — vector ranking ignores time |
| Show me the architecture of system Y. | Entity page synthesis | A pile of partial chunks, no synthesis |

The wiki is the human-readable map. The vector index is the search bar.
Both live at the same path. Both are kept fresh by the same pipeline run.

---

## 11. Different goals, adjacent problems

*Original draft of this section contrasted ourowiki against Karpathy's
sketch as if the two were aiming at the same target. They are not.
Karpathy is publishing patterns to accelerate AI research; ourowiki is
the continuity layer for a working engineer's agent. Different goals
mean the comparison goes in a different direction.*

### 11.1 What Karpathy is doing

Karpathy's LLM-wiki sketch and the wave of public implementations
building on it are aimed, broadly, at AI research workflows: ingest
papers, compile a knowledge base, query it, generate ideas, write
papers, manage citations. The 586 repositories chasing the pattern
as of late April 2026 are mostly tools for AI researchers and
knowledge workers organizing externally-sourced reference material.

It's a worthy target. Accelerating the SOTA in research-agent
tooling has obvious leverage: every researcher who adopts a better
workflow does better research. Karpathy seeds patterns and the
ecosystem amplifies them.

Three implementations stand out as the most substantial executions:

- **`atomicmemory/llm-wiki-compiler`** — a faithful CLI implementation
  of the pattern. `ingest|compile|query|lint|watch|serve`, candidate
  review queue, page-kind schema, epistemic frontmatter, paragraph-
  level claim citations, MCP server.
- **`skyllwt/OmegaWiki`** — a research-lifecycle platform from PKU's
  DAIR Lab. 24 slash commands across paper ingestion, idea generation,
  experiment design, paper writing, and rebuttal. Nine typed entity
  kinds and a daily-arXiv cron.
- **`lucasastorian/llmwiki`** — folder-watcher with a browser UI and
  MCP integration, filesystem-as-source-of-truth, hosted variant at
  llmwiki.app.

These are good systems for the people they're built for. None of
them is the system the present author needed.

### 11.2 What this system is doing

The author of ourowiki is not an AI researcher. He is an engineer
maintaining production codebases (a market-data library, a pub/sub
messaging system, a multicast-aware caching tier, contributions to
an agent runtime) with a stack of side projects on top. The agent
that assists this work has no memory between sessions. Each fresh
conversation begins with the agent re-asking what it should already
know.

That's the operational problem. It is not "organize my reference
library." It is "keep my agent and my future-self oriented across
six months of conversations spanning a dozen projects and two
machines." The shape of the system follows directly:

- **Inputs are conversations, not documents**, because the things
  that need to be remembered are decisions made and bugs found in
  agent sessions, not papers downloaded from arXiv. The corpus
  arrives passively as a side-effect of the user doing their job.
- **Linking is deterministic**, because the LLM is already expensive
  and slow on the synthesis step, and "is `raimd` mentioned in this
  paragraph" is a regex problem.
- **Idempotence is enforced**, because this thing is going to run
  unattended on a cron eventually and any drift compounds across
  weeks before the user notices.
- **Vector retrieval is kept**, because debugging "didn't I see this
  exact stack trace four months ago?" needs partial-recall lookup,
  which is the one thing wiki-style synthesis can't do.
- **The LLM is the prose synthesizer of last resort**, because every
  byte the LLM writes is a byte that has to be cached, invalidated,
  and trusted, and the user has been paid to write deterministic code
  for twenty-five years.

### 11.3 Why the gaps with llm-wiki-compiler are not gaps

Reading the previous draft of this section back, it implicitly
assumed that ourowiki should grow into a Karpathy-faithful
implementation over time — that the absence of an `ingest` verb, a
lint pass, paragraph-level citations, epistemic metadata, a candidate
review queue, an MCP server, and a page-kind schema was a backlog to
work through.

It isn't. Most of those features are answers to questions ourowiki
doesn't ask:

- **`ingest` verb.** ourowiki's input arrives passively. The user
  doesn't have a folder of papers waiting to be ingested. If a paper
  matters, it gets discussed in a conversation; the conversation is
  the source of record. An ingest verb would be solving a problem
  the user doesn't have.
- **Paragraph-level claim citations.** Useful when the synthesis is
  going to be cited in turn (a survey, a lit review, a paper). The
  user's entity pages are read by the user and the user's agent.
  Page-level source attribution is sufficient.
- **Epistemic metadata** (`confidence`, `contradictedBy`,
  `inferredParagraphs`). Useful when consumers downstream need to
  know how trustworthy each page is. The downstream consumer here
  is the user, who already knows.
- **Candidate review queue.** Useful when synthesis errors are
  costly to revert. ourowiki regenerates pages cheaply and the
  user reads what gets generated; review-before-merge would slow
  the loop without adding safety.
- **Page-kind schema.** Useful when the system is general-purpose
  enough to need typed pages. The user's pages are all about
  recurring projects and infrastructure; one shape works.
- **MCP server.** Useful when the system has to plug into multiple
  agent runtimes. The user has one agent runtime he contributes to
  upstream; OpenClaw-shaped is fine.
- **Lint pass.** This one is actually useful and worth building.
  Catching contradictions across daily logs and finding orphan
  entities helps the user and the agent both. It stays on the
  roadmap not because llm-wiki-compiler has it but because the
  user would benefit from it.

The goal isn't to catch up with the reference implementations. The
goal is to keep solving *this* user's problem better.

### 11.4 Where the design choices actually live

Three pieces of engineering in ourowiki are genuinely particular to
the goal and not commodity:

- **Conversation-as-corpus** — the bucketing of sessions into
  human / subagent / automated, the envelope-stripping, the
  largest-variant-wins canonical pick across `.deleted.*` /
  `.reset.*` / `.checkpoint.*` archives, and the per-turn
  summarization layer that makes per-session pages skim-able. None
  of the reference implementations have to do any of this because
  their inputs are static.
- **Two-tier deterministic linking** — Layer 1 (§6.2) resolves
  explicit relations and builds a backlink graph; Layer 2
  (§6.2) scans body prose for unlinked first-mentions of known
  entity tokens, with case heuristics, protection spans, and a
  pre-scan idempotence anchor. The reference implementations
  use LLM-written `[[wikilinks]]`. Both work; ourowiki's choice
  is the one that survives a cron job.
- **Editorial preservation in the master index** (§6.1) — the
  composer regenerates the data-driven sections every run while
  treating user-curated editorial blocks as opaque pass-through.
  This is the pattern that lets the user trust automation without
  losing hand-curation. Reusable beyond ourowiki.

These are the places where the engineering actually went. They are
the parts most likely to be useful to someone with a related problem.

### 11.5 Who might fork this

Not AI researchers. They are well-served by the existing
implementations.

The likely audience is other working engineers with the same
asymmetric memory problem: long-running production work, an
agent that forgets between sessions, no time for hand-curating a
wiki, and no patience for `[[link]] [[everything]] [[explicitly]]`
in LLM-written prose. If that describes you, the parts of this
repo most likely to be reusable in your context are the
conversation-as-corpus pipeline and the two-tier linker. The
editorial-preservation pattern is reusable in a much wider range
of contexts.

If you fork ourowiki and end up at a different design point
because your goals differ from the original author's, that's
working as intended. The goal of publishing this is not a
community around a single artifact; it's an engineered example
of a design point in a crowded space, written down with enough
detail that other engineers can take what's useful and leave the
rest.

### 11.6 Vector search and the "is RAG necessary" question

A running thread in the public landscape is whether vector RAG is
still needed when the LLM has compiled a synthesis. Karpathy's
argument is that at moderate scale it isn't. lucasastorian/llmwiki
agrees in local mode (FTS5, no embeddings). llm-wiki-compiler
disagrees and ships embeddings. ourowiki keeps a fine-tuned local
embedding tier (§10).

The reason ourowiki keeps it: human cognition uses both topic
navigation ("the wiki layer") and time navigation ("last Tuesday")
and partial-recall lookup ("that thing about, what was it, the
linker?"). A topic catalog does the first, a chronological log
the second, embedded retrieval the third. The user's actual
queries against this corpus span all three. Picking only one
breaks the other two.

This is consistent with the design rule above ("the LLM is the
prose synthesizer of last resort"). Embedding-based retrieval is
not LLM-driven; it's a fast index over the corpus. There's no
trade-off between keeping it and minimizing LLM usage.

### 11.7 Comparison table, properly scoped

The table below compares ourowiki against the reference
implementations on dimensions where the comparison is meaningful.
Where a row reads "none" for ourowiki it is *not* a backlog item
unless the design rationale is missing or wrong.

| Dimension | llm-wiki-compiler | OmegaWiki | lucasastorian/llmwiki | ourowiki | Why ourowiki is shaped this way |
|---|---|---|---|---|---|
| Primary input | Documents | Papers | Folder of files | Conversation transcripts | The user's working knowledge lives in agent sessions, not a paper folder |
| `ingest` verb | Yes | Yes | Folder watcher | None | Inputs arrive passively as a side-effect of the user doing their job |
| Cross-link style | LLM-written `[[wikilinks]]` | LLM-written `[[wikilinks]]` | LLM-written | Two-tier deterministic post-pass | Linking is a regex problem; LLM time is better spent on synthesis |
| Idempotence | Hash-based incremental | Not emphasized | Not emphasized | Enforced + verified end-to-end | Has to run unattended on a cron without drift |
| Lint pass | Yes | `/check` | None | None (planned) | Useful for the user; staying on roadmap because the user wants it, not because llm-wiki-compiler has it |
| Paragraph citations | `^[file:42-58]` | Per-claim edges | None | None | Pages aren't being cited downstream by anyone but the user |
| Epistemic metadata | Yes | Claim graph | None | None | The downstream consumer is the user, who already knows |
| Candidate / review queue | `compile --review` | Human gates | None | None | Synthesis errors are cheap to revert; review-before-merge would slow the loop |
| MCP server | Yes | Yes | Yes | None | One agent runtime; portability isn't a goal |
| Page-kind schema | 4 kinds | 9 kinds | None | One implicit kind | All pages are about recurring projects; one shape works |
| Vector retrieval | Configurable embeddings | Implied | FTS5 only (local) | Fine-tuned local embeddings | Partial-recall queries are real; topic + time + recall need separate channels |
| Primary audience | AI researchers, knowledge workers | AI research labs | Personal research workflows | One engineer with shipped production code | The system was built for one specific person |

The "why ourowiki is shaped this way" column is the actual content
of this section. The features ourowiki doesn't have are
overwhelmingly answers to questions the system isn't being asked.

### 11.8 Honest summary

Karpathy and the 586 repos are accelerating AI research tooling.
This paper documents a continuity layer for one engineer's agent.
The two are adjacent in pattern (LLM-synthesized markdown wiki)
and orthogonal in goal.

ourowiki isn't behind llm-wiki-compiler. It isn't ahead of it
either. They're solving different problems with overlapping
techniques. If you're looking for the most faithful Karpathy
implementation, that's llm-wiki-compiler. If you're looking for an
agent-continuity layer for a working engineer, that's this one.

The distinctive engineering — conversation-as-corpus,
two-tier deterministic linking, editorial-preservation in the
composer — is documented because it might be useful to someone
with a related problem, not because it's a competitive moat. The
moat is your goals; the engineering is just what falls out of
taking them seriously.

---

## 12. Complementary to issue/commit-history-driven workflows

A wiki of synthesized entity pages is one half of a project's memory.
The other half lives in version control and issue trackers — the
public record of what got merged, what got reverted, what was filed
as a bug, what was closed as won't-fix, what blocked what. Tools that
drive an agent against that history (clawbot-style issue triage,
GitHub PR review bots, commit-log digesters, release-note generators)
are doing something genuinely different from what ourowiki does, and
the two are best understood as complementary.

A way to draw the split:

- **External record** — GitHub issues, PRs, commits, CI logs, code
  review comments. Public (or at least team-visible). Records *what
  changed* and *who reviewed it*. Time-ordered, immutable, indexed by
  the host platform's search. Tools that reason about this record
  (clawbot, Conventional-commit summarizers, autonomous-PR agents) work
  by walking the history and inferring intent from diffs and comments.

- **Internal record** — conversations between the engineer and the
  agent in which the design got argued out, the bug got chased, the
  approach got abandoned and replaced. Private. Records *what was
  decided* and *why*. Time-ordered if you read transcripts directly,
  topic-ordered if you read the synthesized wiki. Tools that reason
  about this record (ourowiki, lucasastorian/llmwiki, OmegaWiki, etc.)
  work by synthesizing across many conversations into per-topic pages.

Neither half answers the questions the other half answers.

- *"What went into the last release?"* is an external-record question.
  ourowiki has no idea; ask the commit log.
- *"Why does raimd's writer architecture use multiple inheritance
  instead of a vtable?"* is an internal-record question. The commit
  log shows the change; the wiki page captures the discussion that led
  to it.
- *"What's the current state of the MDFieldIter rewrite?"* needs both.
  The wiki page tells you what the engineer was trying and why; the
  branch state and recent commits tell you how far it actually got.

This is a real distinction in practice. An agent helping an engineer
resume dormant work needs both records on hand: the wiki page to
rebuild the conceptual context, the commit/PR history to learn what
the code itself currently looks like. Either alone is incomplete.
The agent reading both is approximately as well-oriented as the
engineer was the day they paused the work.

ourowiki does not try to be the external-record tool. The pipeline
ignores git status, doesn't crawl issues, doesn't cite commits in
entity pages (though sources sometimes mention them in passing).
That's a deliberate scope boundary, not an oversight. The external
record is already an excellent first-class artifact maintained by
GitHub and git itself; tools like clawbot are already excellent
readers of it. ourowiki sits next to those tools, not in their place.

The practical setup an engineer ends up with:

- **clawbot or similar** — maintains the external record, surfaces
  GitHub issues to triage, drafts PRs, summarizes review feedback.
- **ourowiki** — maintains the internal record, surfaces the
  conceptual state of long-running projects, lets a fresh agent (or
  a returning human) pick up where things were left off.
- **A vector index over both corpora** — partial-recall lookup that
  cuts across the topic / time / record-type axes when the user knows
  enough to recognize what they're looking for but not enough to
  navigate to it.

An engineer with all three has a working continuity layer. Removing
any one of them breaks a real query class.

---

## 13. What this system is not

To set expectations precisely:

- **Not a notes app.** No editor, no collaborative UI, no mobile client.
  Inputs are dropped into the filesystem; outputs are read in `less`,
  `vim`, GitHub's markdown render, or piped into an agent's context.
- **Not multi-tenant.** One human, one workspace, one corpus.
- **Not a vendor-neutral memory protocol.** Other systems (Nate B. Jones's
  Open Brain, the MCP memory ecosystem) are pursuing portability between
  AI clients explicitly. This system is local-first and tied to its
  agent runtime's transcript format. Adding an export adapter would be
  straightforward but it isn't here yet.
- **Not a chat history replacement.** The raw JSONL transcripts are the
  source of truth; the wiki is a derived view.
- **Not a fix for an LLM with no working memory.** The wiki helps the
  *user* navigate; the agent reads the wiki the same way a new
  collaborator would.

---

## 14. Roadmap

In descending order of "how much would this make the user's working
life better":

1. **Lint pass.** Catch contradictions across daily logs (claim X on
   date A, contradicted on date B with no resolution), orphan
   entities (in `index.md` cluster list but page never synthesized),
   missing cross-references the synthesis prompt didn't propose,
   broken backlinks. Genuinely an LLM job because most of it is
   semantic; would become the third LLM-touching step. The user has
   already noticed contradictions in `MEMORY.md` that a lint pass
   would have caught.
2. **Shared `wiki_common.py` module.** Four scripts duplicate
   `slugify()` and three share `discover_pages()` and the
   primary-token alias dict. Mechanical refactor that pays off the
   next time anything changes in those shared paths.
3. **Per-week or per-month daily-log rollups.** Sessions are grouped
   by month; daily logs aren't. A weekly rollup with the headlines
   from each day would close the navigation gap.
4. **Public-corpus mode.** A flag that scrubs `MEMORY.md` and
   replaces real names with anonymized handles, suitable for
   showing the pipeline without leaking the user's life. Also
   useful for the repo's example workspace.
5. **Smaller embedding model on smaller hardware.** Make the vector
   tier optional so the wiki alone is useful on a Raspberry Pi or a
   laptop without a fine-tuned bge-small server running.

Notably *not* on the roadmap, despite being standard in the
reference implementations: an `ingest` verb, an MCP server, paragraph-
level citations, epistemic frontmatter, a candidate review queue, a
page-kind schema. §11.3 explains why each one is a non-goal in this
system's context.

If you fork ourowiki and your goals are different from the original
author's, your roadmap will look different. That's expected.

---

## 15. How to read the code

Three orientation paths depending on what's interesting:

- **"How does the LLM call work?"** Read `wiki-turns-summarize.py`
  end-to-end (~210 lines) for the small case, then
  `wiki-entity-pages.py` (~540 lines) for the larger case. Both
  files are async-parallel HTTP plus a JSON cache.
- **"How does idempotence hold?"** Read the `link_implicit_in_page`
  function in `wiki-implicit-links.py`. The pre-scan over existing
  links is the entire idempotence argument distilled to ten lines.
- **"How does the master index work?"** Read `wiki-compose.py`. The
  editorial-section preservation logic is the most reusable pattern
  in the codebase.

The skill file `skills/wiki-maintainer/SKILL.md` documents the same
pipeline from an operator's perspective, including troubleshooting and
common editorial tasks. It and this paper are the two ends of the same
documentation.

---

## 16. Provenance

The system was built in conversation between one human (Chris) and the
agent that wrote this paper (Chesswitch, an OpenClaw-resident assistant)
across roughly twelve weeks. The pipeline graduated from a single
hand-edited `index.md` into eleven scripts as the data outgrew what one
file could carry. Every script in the pipeline was discussed and
prototyped in a session whose transcript is itself in the corpus the
pipeline indexes — the system is, in the most literal sense, recursively
self-aware about its own construction.

The first pull request to the upstream agent runtime that referred to
"the wiki layer" landed in February 2026 as a documentation patch about
multi-machine config sync. Layers 1 and 2 of the entity-linker were
written and integrated on April 29 and 30, 2026, hours before this
paper was drafted.

---

*This paper is intended as the README of a future public repository for
the wiki tooling. Suggestions, forks, and competing designs welcome.*
