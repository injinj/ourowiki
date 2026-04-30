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

Most personal "second-brain" or "memory" systems for large language models
fall into one of two camps:

1. **Vector retrieval over raw notes.** Embed every chunk, do nearest-
   neighbour lookup at query time, paste hits into context. Works at any
   scale. Returns fragments, not understanding.
2. **Hand-curated wiki / Zettelkasten.** Human writes pages. The LLM is just
   a reader. Beautiful structure, terrible scaling: no human writes 200
   well-cross-linked pages a year on top of a day job.

The system described in this paper is a third thing: a **two-tier
markdown wiki where the top tier is synthesized and maintained by an LLM
from raw conversation transcripts and daily journal notes**, with
deterministic post-passes that resolve cross-references and enforce
idempotence. It sits next to a vector index, not in place of one — the
two retrieve different things and complement each other.

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

## 11. Contrast with Karpathy's "LLM wiki"

Andrej Karpathy has sketched a personal-knowledge architecture in public
talks and tweets that's the closest published analogue to this system.
Roughly: an LLM-owned directory of markdown files, an `index.md` content
catalog, a `log.md` chronological record, three operations (`ingest`,
`query`, `lint`), and a schema doc (`CLAUDE.md` / `AGENTS.md`) describing
how the LLM should behave. Karpathy's framing emphasizes that at moderate
scale (hundreds of pages) **synthesis + a hand-readable catalog can
replace vector RAG entirely**, because the synthesis already happened.

This system started from that sketch and diverged in five concrete ways.

### 11.1 Inputs are conversation transcripts and daily notes, not "documents"

Karpathy's worked example is dropping a paper or article into the corpus.
The "ingest" operation reads the paper and updates 10–15 wiki pages.

This system's primary input is JSONL conversation transcripts produced by
an agent runtime — an immutable, append-only record of every back-and-forth
between human and assistant, including tool calls, errors, retries, and
abandoned threads. Plus a human-authored daily log. The papers, PDFs, and
articles are *referenced* in those transcripts but they aren't the corpus.

This shifts the pipeline. The bulk of the engineering goes into:

- Bucketing sessions (human / subagent / automated)
- Stripping envelope metadata from messages
- Recovering archived variants (`.deleted.*`, `.reset.*`)
- Per-turn summarization so the per-session pages are skim-able

None of that exists in Karpathy's sketch, because his inputs are static
documents.

### 11.2 Two LLM steps, not one

Karpathy treats "ingest" as a single LLM operation that updates the
wiki holistically. This system splits it into per-turn summarization
and per-entity synthesis, each individually cached, with deterministic
glue between them. The per-turn step gives every conversation a
skim-friendly outline; the per-entity step gives recurring topics a
synthesized article. Different prompts, different cache keys, different
costs, different failure modes.

### 11.3 Caching is content-addressed, not freshness-based

Karpathy's pitch implies the LLM re-runs ingest on each new source
arriving, and that the system is otherwise "kept current" by the LLM.
This system instead defines: for each entity, the cache key is the
sha256 of the concatenated normalized source texts. Add a new source
and the hash changes; that entity gets re-synthesized. Existing entities
with unchanged sources don't move. This makes "keeping the wiki current"
a one-liner cron job and removes the LLM from the freshness loop.

### 11.4 Linking is not the LLM's job

Karpathy's wiki is "LLM-maintained," including cross-references. This
system explicitly does **not** ask the LLM to maintain links. The LLM
proposes related-name strings in plain text (in the `## Cross-references`
section). Two deterministic post-passes resolve those into actual
links and discover implicit cross-references in body prose:

- **Layer 1 (`wiki-cross-refs.py`):** explicit link resolution + backlink
  graph, both keyed on a hand-curated alias map. ~500 lines of Python,
  no LLM.
- **Layer 2 (`wiki-implicit-links.py`):** body-text scan for known entity
  tokens, with case heuristics, protection spans (code, headings,
  Layer-1-owned regions), and an idempotence anchor (pre-scan existing
  links to make repeated runs no-ops). ~520 lines of Python, no LLM.

Putting linking in code rather than the LLM is a practical bet:
linking is a small set of deterministic problems (alias resolution,
graph traversal, regex scan with protected regions). Letting the LLM
do them costs money, introduces drift, and makes diffs noisy. Letting
code do them is fast, free, and inspectable.

This may be the largest specific divergence from Karpathy's framing.
He treats the LLM as the curator-of-record. This system treats the LLM
as the **prose synthesizer of last resort**, surrounded by
deterministic infrastructure that does everything the LLM doesn't have
to.

### 11.5 Vector search lives next door, not in opposition

Karpathy's claim that vector RAG is unnecessary at moderate scale is
defensible for some audiences. This system keeps both. The wiki is
better at "what is `raimd`?"; vector search is better at "did I ever
mention `RwfMsgKeyWriter`?"; the two channels are cheap to keep and
serve different queries.

The reason to keep both: human cognition uses both. People navigate
their own memory by topic ("my house") and by time ("last Tuesday")
and by partial recall ("that thing about, what was it, the linker?").
A topic catalog does the first, a chronological log the second,
embedded retrieval the third. Picking only one breaks the other two.

### Summary table

| Dimension | Karpathy's sketch | This system |
|---|---|---|
| Primary input | Static documents | Conversation transcripts + daily notes |
| LLM operations | Holistic ingest | Per-turn summary + per-entity synthesis |
| Cache strategy | Implied freshness | Content-hash, two independent caches |
| Cross-references | LLM-maintained | Deterministic post-pass |
| Backlinks | Not specified | Auto-generated graph in every page |
| Implicit linking | Not specified | Layer 2: deterministic, idempotent, case-aware |
| Vector retrieval | Argued unnecessary | Kept; orthogonal channel |
| Schema doc | `CLAUDE.md` / `AGENTS.md` | `AGENTS.md` + `SOUL.md` + `IDENTITY.md` + `USER.md` + `TOOLS.md` |
| Pipeline shape | Conceptual (3 ops) | 11 explicit scripts, ~4000 LOC |
| Idempotence | Implied | Enforced; verified end-to-end |
| Cost (steady state) | Unstated | $0 + a few seconds |

The takeaway is not that Karpathy is wrong. The takeaway is that the
sketch has more concrete engineering inside it than the sketch lets on,
and most of that engineering wants to live in deterministic code, not
the LLM.

---

## 12. What this system is not

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

## 13. Roadmap

The directions a public iteration is most likely to move:

1. **Shared `wiki_common.py` module** — four scripts duplicate
   `slugify()` and three more share `discover_pages()` and the
   primary-token alias dict. Extracting them is mechanical.
2. **Lint pass** — Karpathy's third operation. Detect contradictions
   inside an entity page (claim X on date A, contradicted on date B
   with no resolution), orphan entities (in `index.md` cluster list
   but page never synthesized), missing cross-references that the
   LLM didn't propose. This is genuinely an LLM job because it's
   semantic.
3. **Per-day rollup pages** — sessions group by month; daily logs
   should also have per-week or per-month rollups for navigation.
4. **Public-corpus mode** — a flag that scrubs `MEMORY.md` and
   replaces real names with anonymized handles, suitable for
   showing the system's pipeline without leaking the user's life.
5. **Adapter for non-JSONL transcript formats** — generalize the
   session ingester so other agent runtimes (a stripped-down
   "transcript adapter" with `.parse_session(path) → list[Turn]`)
   can plug in.
6. **Smaller embedding model on smaller hardware** — current setup
   assumes a fine-tuned bge-small on CPU. Make the vector tier
   optional so the wiki alone is useful on a Raspberry Pi.

---

## 14. How to read the code

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

## 15. Provenance

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
