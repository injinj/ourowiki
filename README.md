# ourowiki

> *An LLM-synthesized personal wiki that indexes the conversations that built it.*

A self-maintaining markdown wiki layered on top of an agent's
conversation transcripts and daily notes. Two LLM steps for synthesis,
nine deterministic steps for everything else, content-addressable
caching throughout, idempotent end-to-end.

```
                  raw conversation transcripts
                     + daily journal notes
                              │
                              ▼
                deterministic extracts (TSVs)
                              │
                              ▼
              LLM #1 — per-turn summaries (cached)
              LLM #2 — per-entity synthesis (cached)
                              │
                              ▼
            deterministic render + cross-link passes
                              │
                              ▼
        a fully linked, browsable, vector-indexable wiki
                  that re-runs in ~20 s warm
```

The name is *ouroboros* + *wiki* — the system indexes the conversations
that built it, including the conversation that named it.

## What is this for?

This is the continuity layer for a working engineer who uses an AI
assistant heavily across long-running production projects and a stack
of side projects. The author maintains shipped codebases, runs work
across multiple machines, and talks to an agent that has no memory
between sessions. The system exists so that the user (and the agent
on the user's behalf) can recall what was decided about which project
six weeks ago without re-reading every transcript.

It is **not** a research tool. It does not ingest papers. It has no
opinion about the SOTA. It is what falls out when an engineer notices
that their agent's amnesia is becoming an operational problem and
builds the smallest deterministic layer that fixes it.

If you're looking for a Karpathy-faithful implementation of the
LLM-wiki pattern with `ingest` / `compile` / `query` / `lint` verbs,
an MCP server, and paragraph-level claim citations, see
[`atomicmemory/llm-wiki-compiler`](https://github.com/atomicmemory/llm-wiki-compiler)
or [`skyllwt/OmegaWiki`](https://github.com/skyllwt/OmegaWiki). Those
are different (and excellent) systems aimed at AI research workflows.
This one is shaped around a different goal. The white paper's §11
lays out where and why.

## Features

- **Two-tier markdown wiki** — entity pages plus a master index.
- **Per-session pages** — three views per conversation (one-line summary
  per turn, full prose, full transcript with tool calls).
- **Per-entity synthesis** — Karpathy-style one-page-per-topic articles,
  cached by source-content hash.
- **Auto cross-references** — explicit `- related: <name>` resolution
  plus a backlink graph on every page.
- **Auto implicit linking** — first mention of a known entity in any
  page's body prose becomes a clickable link, idempotent across re-runs.
- **Largest-variant-wins** session picking — robust to runtime archives
  (`.deleted.*`, `.reset.*`, `.checkpoint.*` files).
- **Editorial preservation** — composer regenerates the deterministic
  parts of `index.md` every run while leaving human-curated sections
  untouched.

## How big is it?

About 4,000 lines of Python and shell across eleven scripts. No
dependencies beyond a recent Python and `httpx`. No database. No
service. Plain markdown on disk.

## Read the architecture

The full design — data flow, LLM prompts, caching strategy, three
flagship algorithms (editorial preservation, two-layer entity-linker,
canonical session pick), failure modes, performance numbers, and an
explicit five-way contrast with Karpathy's "LLM wiki" sketch — lives
at:

📄 [`docs/wiki-architecture.md`](docs/wiki-architecture.md)

If you only read one section, read **§11 (Contrast with Karpathy's
"LLM wiki")** for the design ideology, and **§6 (Algorithms)** for the
parts that took the most thought.

## Status

Working system, in production over twelve weeks of conversation
transcripts. Just made public — the goal of opening it up is to attract
forks and competing designs, not to ship a polished product. Interfaces
will move. Caches may invalidate. The pipeline order may shake out.

## Running it

ourowiki currently expects an
[OpenClaw](https://github.com/openclaw/openclaw)-shaped workspace:

```
~/.openclaw/workspace/
├── MEMORY.md
├── memory/
│   ├── YYYY-MM-DD.md         # daily journal files
│   └── …
└── ~/.openclaw/agents/main/sessions/
    └── <uuid>.jsonl          # one JSONL line per agent turn
```

Generalizing the input adapter to other agent runtimes is on the
[roadmap](docs/wiki-architecture.md#13-roadmap) (§13.5).

Quick path:

```bash
export ANTHROPIC_API_KEY=...           # required by steps 3 and 8
cd /path/to/your/openclaw-workspace
git clone https://github.com/<you>/ourowiki .ourowiki

bash    .ourowiki/scripts/wiki-extract.sh
python3 .ourowiki/scripts/wiki-turns-extract.py
python3 .ourowiki/scripts/wiki-turns-summarize.py    # LLM, cached
python3 .ourowiki/scripts/wiki-sessions-compose.py
python3 .ourowiki/scripts/wiki-month-pages.py
python3 .ourowiki/scripts/wiki-compose.py
python3 .ourowiki/scripts/wiki-entity-pages.py       # LLM, cached
python3 .ourowiki/scripts/wiki-cross-refs.py
python3 .ourowiki/scripts/wiki-implicit-links.py
python3 .ourowiki/scripts/wiki-link-topics.py
```

(Or symlink `.ourowiki/scripts` into your workspace as `scripts/`.)

The first cold run takes a few minutes and costs about USD $0.05–$0.10
in Claude Haiku calls. Subsequent runs are seconds and free.

## Operator's guide

[`skills/wiki-maintainer/SKILL.md`](skills/wiki-maintainer/SKILL.md) is
the operator's manual: per-step troubleshooting, common editorial
tasks, idempotence verification, and the conventions for adding new
entity pages and topic clusters. Written for an agent to follow but
fully readable as documentation.

## Forking

This repo is intentionally bare. No `CONTRIBUTING.md` yet, no roadmap
issues, no project board. The first thing it wants is to be **forked
and re-shaped** by people whose memory looks different from mine. If
you fork it and learn something, open an issue and tell me.

A few directions that look interesting:

- A transcript adapter for non-OpenClaw agent runtimes
- A `wiki_common.py` extract for the four scripts that duplicate
  `slugify()` and friends
- The lint pass — Karpathy's third operation, which we don't have yet
- A "public-corpus mode" flag that scrubs personal context before
  publishing a generated wiki as a portfolio piece
- Replace Claude Haiku with a local model and report results
- Swap markdown for org-mode, or the other way

## License

[Apache License 2.0](LICENSE).

## Etymology

`ouroboros` (the serpent eating its own tail) + `wiki`. The system
synthesizes pages from conversation transcripts that include the
conversations in which the system itself was designed and named. Every
re-run reads its own architectural rationale as a source. It's
recursion all the way down, and at some point that becomes load-bearing
rather than cute.
