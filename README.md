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

This is a **shared continuity layer** for two parties with
complementary memory limits:

- **The agent** has zero session-to-session memory. Every fresh
  conversation starts cold.
- **The human** has gradient memory loss across a stack of
  long-running projects. When a dormant project comes back — *what
  state is `raimd` in? what was the last thing I tried with the
  virtual-dispatch refactor?* — the human can no more recall the
  specifics than the agent can.

ourowiki produces one entity page per recurring project, synthesized
from the conversation transcripts where the work actually happened.
When the human comes back to a dormant project, they read the page
to rebuild context. When the agent starts fresh and needs context to
be useful, it reads the *same page*. Same artifact, two readers,
both refreshed by the same pipeline.

This is the design's distinguishing feature. Most LLM-wiki systems
treat the wiki as a thing the LLM produces for the LLM (the cache
that lets it skip RAG) or a thing the human produces for the human
(a Zettelkasten with an LLM helper). ourowiki treats it as the
*single* view of project state that both parties draw from. The
fact that an LLM does the synthesis is an implementation detail.

ourowiki is **complementary**, not competitive, to issue / commit /
PR-driven agent workflows — e.g. [ClawSweeper](https://github.com/openclaw/openclaw/blob/main/.github/workflows/clawsweeper-dispatch.yml)
(an issue-triage bot in the OpenClaw upstream repo) and the broader
class of release-note generators, autonomous-PR agents, and
commit-log digesters. Those tools maintain the *external* record of
a project (what got merged, what's broken, what's blocked);
ourowiki maintains the *internal* record (what was decided and why).
An engineer resuming dormant work needs both. See the white paper's
§12 for the full split.

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

## Quickstart

ourowiki expects an
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
[roadmap](docs/wiki-architecture.md#13-roadmap).

### One-step install

```bash
git clone https://github.com/injinj/ourowiki ~/code/ourowiki
cd ~/code/ourowiki
./install.sh
```

The installer prompts for provider (`anthropic` / `openai` /
`openai-compat`), API key, model, OpenClaw workspace path, and a cron
expression. It writes `~/.ourowiki/env` (mode 600), runs the pipeline
once so the first wiki regen is on disk before you walk away, and
appends a daily-cron entry that re-runs the pipeline (incremental,
free on cache hits).

All prompts are skippable via flags:

```bash
./install.sh \
  --provider openai \
  --model gpt-5-mini \
  --api-key sk-... \
  --workspace ~/.openclaw/workspace \
  --cron "0 4 * * *" \
  --yes
```

Use `--no-cron` to skip cron registration; `--no-run` to skip the
first-run pipeline pass.

### Provider configuration

Selection is driven by environment variables:

| Variable | Purpose |
|---|---|
| `OUROWIKI_PROVIDER` | `anthropic` (default) / `openai` / `openai-compat` |
| `OUROWIKI_MODEL`    | Model id override (per-script default otherwise) |
| `ANTHROPIC_API_KEY` | Required when provider=anthropic |
| `OPENAI_API_KEY`    | Required when provider=openai or openai-compat. Any non-empty string works for local servers that don't authenticate. |
| `OPENAI_BASE_URL`   | Custom base URL (e.g. `http://localhost:8080/v1` for llama-server, OpenRouter URL, etc.). |
| `ANTHROPIC_BASE_URL`| Custom Anthropic-compatible endpoint, optional. |

**gpt-5 / o1 / o3 caveat:** reasoning models burn most of the
`max_completion_tokens` budget on hidden reasoning tokens before
producing visible content. ourowiki auto-detects these model families
and bumps the per-call budget to 4096 tokens minimum so the response
isn't empty. If you're hitting empty responses with another
reasoning-class model not on the auto-detect list (`gpt-5*`, `o1*`,
`o3*`, `o4*`), edit `_REASONING_MODEL_PREFIXES` in
`scripts/wiki_provider.py`.

### Manual invocation (no install.sh)

If you'd rather not run a setup script, the pipeline is just eleven
ordered commands:

```bash
export OUROWIKI_PROVIDER=openai
export OPENAI_API_KEY=sk-...
export OUROWIKI_MODEL=gpt-5-mini
export OPENCLAW_WORKSPACE=~/.openclaw/workspace
cd $OPENCLAW_WORKSPACE

bash    ~/code/ourowiki/scripts/wiki-extract.sh
python3 ~/code/ourowiki/scripts/wiki-turns-extract.py
python3 ~/code/ourowiki/scripts/wiki-turns-summarize.py    # LLM, cached
python3 ~/code/ourowiki/scripts/wiki-sessions-compose.py
python3 ~/code/ourowiki/scripts/wiki-month-pages.py
python3 ~/code/ourowiki/scripts/wiki-compose.py
python3 ~/code/ourowiki/scripts/wiki-entity-pages.py       # LLM, cached
python3 ~/code/ourowiki/scripts/wiki-cross-refs.py
python3 ~/code/ourowiki/scripts/wiki-implicit-links.py
python3 ~/code/ourowiki/scripts/wiki-link-topics.py
```

### Cost & runtime

First cold run: ~3–5 minutes wall-clock, ~$0.05–$0.10 in LLM calls
(Anthropic Haiku rates; OpenAI gpt-5-mini is in roughly the same
ballpark). Subsequent runs hit the content-hash cache for ~100% of
turns and entities, finishing in seconds at zero LLM cost.

### Validated provider configurations

Validated end-to-end against a fresh isolated workspace as of
2026-04-30:

- `OUROWIKI_PROVIDER=anthropic` with `claude-haiku-4-5` (original)
- `OUROWIKI_PROVIDER=openai` with `gpt-5-mini`

Not yet smoke-tested but expected to work given they speak the same
API shape: `OUROWIKI_PROVIDER=openai-compat` against llama-server,
Ollama (OpenAI-compatible mode), OpenRouter, LiteLLM. If you
verify any of these, please open an issue with model + base URL so
we can list it here.

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
