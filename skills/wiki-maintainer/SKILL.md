---
name: wiki-maintainer
description: "Regenerate, audit, or extend the LLM Wiki layer at memory/index.md + memory/sessions/. Use when memory grows, when index.md or per-session pages are stale, or when the user asks to refresh / audit / expand the wiki."
metadata:
  {
    "openclaw":
      {
        "emoji": "📚",
        "requires": { "bins": ["bash", "jq", "python3"], "env": ["ANTHROPIC_API_KEY"] }
      }
  }
---

# Wiki Maintainer Skill

Maintain the **Karpathy-style LLM Wiki** layered over OpenClaw's memory system. The wiki is two things in one:

- **Index** (`memory/index.md`) — one-line catalog of every daily file, every session, MEMORY.md sections, and curated topic clusters
- **Per-session detail pages** (`memory/sessions/<uuid>.md`, `.full.md`, `.tools.md`) — turn-by-turn summaries, full prose transcripts, and tool-call transcripts for every human-driven session

Together they give a structural / chronological browseable view that complements the vector-based semantic search.

## What the wiki contains

```
memory/
├── index.md                           # top-level catalog (~140 lines)
├── 2026-04-29.md                      # daily journal entries (raw human notes)
├── ...
└── sessions/
    ├── 2026-02.md                     # monthly session indexes
    ├── 2026-03.md
    ├── 2026-04.md
    ├── <uuid>.md                      # per-session q/a summary (LLM-summarized via Haiku)
    ├── <uuid>.full.md                 # prose transcript: user prompts + assistant prose
    ├── <uuid>.tools.md                # transcript + tool calls / results in <details> folds
    └── .summaries.json                # persistent cache for LLM summaries (keyed by msg_id)
```

The `index.md` workflow is **daily → monthly session page → per-session detail → full transcript** — designed for opening multiple windows side-by-side when re-reading work.

## When to use

✅ **USE this skill when:**
- User asks to regenerate, refresh, rebuild, or audit the wiki
- User adds significant new daily entries or sessions (even one new daily counts)
- After session indexing changes (sources added, reindex forced)
- Periodically (daily-ish ideal) as part of memory hygiene
- User mentions "wiki", "memory index", "topic clusters", "session summaries"

❌ **DO NOT use this skill when:**
- User just wants to write to today's daily file → that's `memory/YYYY-MM-DD.md`, edit directly
- User wants to add to long-term memory → that's `MEMORY.md`, edit directly
- User wants single-file edits to existing wiki content → just edit (the composer preserves editorial regions)
- The vector index is the right tool (semantic search)

## Pipeline architecture

Ten scripts under `~/.openclaw/workspace/scripts/`, run in order:

| # | Script | What it does | Cost / time |
|---|---|---|---|
| 1 | `wiki-extract.sh` | Build TSVs of dailies + classify sessions (human / subagent / auto). Handles modern + early-format sessions, hybrid sessions, multi-day continuations. | Fast (seconds) |
| 2 | `wiki-turns-extract.py` | Pull per-turn `(user, assistant)` pairs from each human session into `/tmp/wiki-build/turns/<uuid>.jsonl`. | Fast |
| 3 | `wiki-turns-summarize.py` | Async parallel calls to Claude Haiku 4.5 with persistent cache. **Requires ANTHROPIC_API_KEY in env.** | First run slow (1-3 min for ~800 turns); cache makes re-runs instant |
| 4 | `wiki-sessions-compose.py` | Render `<uuid>.md` per-session summary pages, plus shell out to wiki-session-reconstruct.py for `.full.md` and `.tools.md` companion files. | Seconds |
| 5 | `wiki-session-reconstruct.py` | Build full prose / tools / raw transcripts. Called by step 4 for every session; also usable on demand for one session. | Per-session: <1s |
| 6 | `wiki-month-pages.py` | Generate `sessions/YYYY-MM.md` with all sessions for that month. | Fast |
| 7 | `wiki-compose.py` | Write `memory/index.md` from extracted data + previous editorial sections. Idempotent. | Fast |
| 8 | `wiki-entity-pages.py` | Synthesize `memory/wiki/<slug>.md` per topic cluster via Haiku. Cached by source content-hash. **Requires ANTHROPIC_API_KEY.** | First run ~1 min for 10 clusters; cache makes re-runs instant |
| 9 | `wiki-cross-refs.py` | **Layer 1 entity-linking.** Resolve `- related: <name>` lines in entity pages to wiki-page links and inject auto-generated `## Backlinks` sections. Deterministic (no LLM). Idempotent. | Fast |
| 10 | `wiki-implicit-links.py` | **Layer 2 entity-linking.** Scan synthesized body text of each `wiki/<slug>.md` for unlinked mentions of known entity names (single-token slugs + hand-picked primary tokens) and link the first occurrence per (page, target). Skips code, headings, and the Cross-references / Backlinks / sources sections. Deterministic. Idempotent (re-runs are no-ops because each target is linked at most once per page). | Fast |
| 11 | `wiki-link-topics.py` | Add clickable links to dates / session uuids / MEMORY.md refs / wiki entity pages in the editorial sections of `index.md`. Idempotent. | Fast |

After all 11: `openclaw memory index` to refresh the vector index.

## Quick path (typical regen)

```bash
. ~/.openclaw/env.sh   # loads ANTHROPIC_API_KEY (required by steps 3 and 8)
cd ~/.openclaw/workspace

bash    scripts/wiki-extract.sh
python3 scripts/wiki-turns-extract.py
python3 scripts/wiki-turns-summarize.py  --concurrency 8 --save-every 25
python3 scripts/wiki-sessions-compose.py
python3 scripts/wiki-month-pages.py
python3 scripts/wiki-compose.py
python3 scripts/wiki-entity-pages.py     --concurrency 4
python3 scripts/wiki-cross-refs.py
python3 scripts/wiki-implicit-links.py
python3 scripts/wiki-link-topics.py
openclaw memory index
```

If everything is up to date, the run is cheap because:
- The turn summarizer cache hits ~100% of turns on re-runs
- The entity synthesizer cache hits ~100% of pages on re-runs (keyed by source content-hash)
- The composer is idempotent
- `wiki-link-topics.py` is idempotent (skips already-linked text)

Verification helpers:
```bash
python3 scripts/wiki-compose.py --check         # exit 0 if up-to-date
python3 scripts/wiki-entity-pages.py --dry-run  # show what would be synthesized
python3 scripts/wiki-link-topics.py --dry-run   # preview link diffs without writing
```

## Detailed pipeline notes

### Step 1: `wiki-extract.sh`

Outputs to `/tmp/wiki-build/`:
- `dailies.tsv` — `date<TAB>topic1|topic2|...`
- `sessions-human.tsv` — `date<TAB>uuid<TAB>bytes<TAB>first-user-message`
  - Long sessions spanning multiple local dates emit multiple rows, with `[continuation]` tagging on later dates (cap: 24 continuation rows per session)
  - The `date` column uses the **local date** parsed from the `[Day YYYY-MM-DD HH:MM TZ]` envelope (not UTC) so cross-midnight sessions don't split
  - For each date, picks the first **non-cron-callback** message as the headline
  - Early-format sessions (predating the `[Day...]` envelope) get a single row with their first non-auto user message
- `sessions-subagent.tsv` — subagent task transcripts
- `sessions-auto.tsv` — cron / system / heartbeat sessions
- `summary.env` — pre-rendered editorial fragments (Coverage line, Last regenerated, etc.) the composer drops in verbatim
- `summary.txt` — human-readable counts

File-variant logic: picks the **largest** of `<uuid>.jsonl`, `<uuid>.jsonl.reset.*`, `<uuid>.jsonl.deleted.*`, `<uuid>.checkpoint.*.jsonl` — handles cases where the live `.jsonl` was rotated to a stub.

Auto markers (skipped from human bucket): `[cron:`, `System:`, `[Subagent Context]`, `Begin. Your assigned task`, `You are running as a subagent`, `A cron job "..." just completed`, `A subagent task "..."`, `Cron:`, `NOTIFY_USER:`, `[OpenClaw heartbeat poll]`, `<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>`, `Sender (untrusted metadata):`.

### Step 2: `wiki-turns-extract.py`

Reads `sessions-human.tsv`, walks each session file, pairs user → next-assistant turns. Writes `<uuid>.jsonl` with cache-stable keys (`<uuid>:<msg_id>`).

Handles two formats:
- **Modern:** user message contains a `[Day YYYY-MM-DD HH:MM TZ]` envelope on a line of any text part (multi-part user messages are scanned)
- **Early-format:** session has no envelopes anywhere — fall back to "first text part of each user msg, classified by first-line auto markers"

Skips: assistant turns containing `NO_REPLY`, `[assistant turn failed`, `⚠️ Agent failed`, `HEARTBEAT_OK`. Skips user metadata blocks (`Sender (untrusted metadata):`, internal context wrappers).

Truncates assistant text to 4000 chars to keep summarizer prompts cheap.

### Step 3: `wiki-turns-summarize.py`

Async Claude Haiku 4.5 calls (default concurrency 8). Each turn gets a one-line summary (≤ 22 words).

**Persistent cache** at `memory/sessions/.summaries.json` keyed by `<uuid>:<msg_id>`. Saves every 25 fresh summaries (so a crash doesn't lose progress). Cache is safe to delete to force re-summarization, safe to hand-edit to refine wording across regens.

Auth: requires `ANTHROPIC_API_KEY` in env (sourced from `~/.openclaw/env.sh`).

CLI flags worth knowing:
- `--limit-per-session N` — smoke test
- `--only-uuid <uuid>` — re-summarize one session
- `--model claude-haiku-4-5` — default; can swap providers
- `--concurrency 8` — adjust based on rate limits

Failure handling: 429/5xx retried with exponential backoff. Other errors logged and the turn marked with empty summary (rendered as `_(no summary)_`).

### Step 4: `wiki-sessions-compose.py`

Writes three files per session into `memory/sessions/`:

1. `<uuid>.md` — q/a summary list grouped by local date. Each turn:
   ```
   - **HH:MM**
     - **q:** user prompt (trimmed to ~150 chars)
     - **a:** LLM summary
   ```
2. `<uuid>.full.md` — prose transcript (user prompts + assistant prose, no tool noise) via `wiki-session-reconstruct.py --mode prose`
3. `<uuid>.tools.md` — full transcript with collapsible `<details>` blocks for tool calls and tool results via `--mode tools`

### Step 5: `wiki-session-reconstruct.py` (also a one-shot CLI tool)

Standalone usage when you want to read one session right now:

```bash
# Plain prose (good for re-reading a conversation as a story)
python3 scripts/wiki-session-reconstruct.py 0a7719b7 --mode prose | less

# With tool calls + results
python3 scripts/wiki-session-reconstruct.py bdd06613 --mode tools

# Lossless raw dump (every JSONL record pretty-printed)
python3 scripts/wiki-session-reconstruct.py 336366bc --mode raw -o /tmp/full.md
```

Short uuid prefix (8 chars) is auto-resolved.

Tool-call rendering reads `arguments` (OpenClaw runtime field), with `input` and `params` fallbacks for other transports.

User-turn rendering has the same modern + early-format dual mode as the turn extractor: if any user message in the session has a `[Day...]` envelope, it uses envelope-mode (only those messages render). Otherwise it falls back to early-format mode where any user message that doesn't start with an auto marker is treated as a human turn, with the timestamp pulled from the message's UTC timestamp. Without this fallback, early-format sessions (pre-`[Day...]`-envelope, mostly Feb 2026) would show 0 user turns.

### Step 6: `wiki-month-pages.py`

Reads `sessions-human.tsv`, groups by `YYYY-MM`, writes one `sessions/YYYY-MM.md` per month with every session row (and its triple-link to detail/full/tools).

### Step 7: `wiki-compose.py`

Writes `memory/index.md`. Sections:

**Deterministic (regenerated every run):**
- Header / metadata block (Coverage, Last regenerated)
- 📅 Daily logs — one bullet per daily file, grouped by month, newest first. Each month header includes a 📂 Sessions link to that month's session page. Date headlines are clickable links to the daily file (only when the file exists).
- 🧠 Long-term memory (`MEMORY.md`) — title and each section bullet linked to anchors on `../MEMORY.md`
- 💬 Sessions — collapsed to monthly links (e.g. `[April 2026](sessions/2026-04.md) — 38 rows · 26 unique sessions`)
- 🤖 Sessions — automated — paragraph from `summary.env`
- 🧪 Sessions — subagent tasks (when count > 0)
- Footer

**Editorial (preserved verbatim from prior `index.md`):**
- 🪞 About this index
- 🔖 Topic clusters
- Per-date polished daily headlines (after the em-dash) — preserves LLM polish across regens

The composer has anti-clobber logic for two formats of editorial daily bullets:
- Bare bold: `- **2026-04-29** — ...`
- Linked: `- [**2026-04-29**](2026-04-29.md) — ...`

It always re-emits in the linked form when the daily file exists, but preserves the post-em-dash body verbatim.

### Step 8: `wiki-entity-pages.py`

Synthesizes per-topic Karpathy-style entity pages at `memory/wiki/<slug>.md` from each cluster bullet's source list.

For each cluster bullet in `🔖 Topic clusters`:
1. Parse the cluster name and the linked source files (dailies, sessions, MEMORY.md anchors).
2. Read source contents (daily files raw, session detail pages from step 4, MEMORY.md sections by anchor).
3. Compute a content-hash over normalized sources. If the hash matches the cache, skip.
4. Otherwise call Claude Haiku with a structured system prompt that produces:
   - H1 entity name + blockquote elevator pitch (≤25 words)
   - Status / first-mention / last-mention line
   - `## What it is` (2-4 paragraphs of synthesis)
   - `## Key events / decisions` (chronological bullets, each starting with `**YYYY-MM-DD** —`)
   - `## Open questions / next steps` (only if applicable)
   - `## Cross-references` (other entities mentioned, prefixed `- related: <name>`)
5. Wrap with a sources footer linking back to the synthesized inputs and a content-hash stamp.

**Cache:** `memory/wiki/.entity-cache.json` keyed by slug. Stores content hash, synthesis timestamp, model, source count, token usage.

**Slug derivation:** lowercase, strip parens (descriptive subtitle), strip backticks/asterisks/underscores keeping inner text, replace runs of non-alnum with hyphens. ``"`raimd` (C/C++ market data library)"`` → `raimd`.

Auth: requires `ANTHROPIC_API_KEY` in env (sourced from `~/.openclaw/env.sh`).

CLI flags:
- `--only <substring>` — synthesize one cluster matching name or slug
- `--force` — ignore cache, re-synthesize all
- `--dry-run` — print what would change
- `--concurrency 4` — parallel synthesis (default 4)
- `--model claude-haiku-4-5` — default; can swap providers

**Costs:** at Haiku rates, 10 clusters with average 4-7 sources each totals ~120K input + ~13K output tokens ≈ USD $0.05-0.10 per full re-synthesis. Cache makes incremental re-runs essentially free.

### Step 9: `wiki-cross-refs.py`

Resolves `- related: <name>` lines in `wiki/<slug>.md` Cross-references sections to wiki-page links, and injects auto-generated `## Backlinks` sections. **Deterministic (no LLM)**. Idempotent.

**What it does:**
1. Parses every `wiki/<slug>.md`, extracts entity name (from H1) and `## Cross-references` bullets.
2. Builds a name → slug alias map combining: page slugs, H1 titles, cluster names from `index.md`, and a small hand-picked dictionary of one-word "primary subject" tokens (e.g., `raims` → `networking-multicast-rvd-raims`, `chex` → `chex-hardware`, `MDMsgWriter` → `raimd`).
3. For each `- related: <name>` line, resolves `<name>` against the alias map. If matched (and not the page itself), rewrites as `- related: [<name>](<slug>.md)`. Already-linked names are left alone but counted toward target sets.
4. Builds reverse map (incoming edges per page) from resolved links.
5. Injects a `## Backlinks` section between Cross-references and the sources footer for every page that has at least one incoming link. Sorts alphabetically by source title. Section is canonical-formatted and idempotent.

**Scope (Layer 1 only):** Only operates on existing `## Cross-references` bullets. Implicit body-text cross-references ("raimd" mentioned in prose without a link) are handled by Layer 2 — see step 10 below.

**Hand-picked aliases** live inline in `build_alias_map()`. When a new entity page is added, the slug + H1 + cluster name are picked up automatically; only add a primary-token entry if you want to resolve bare one-word references like `raims` to a specific page.

**Self-references suppressed:** if a page's Cross-references mentions its own primary subject (e.g., `raimd.md` mentioning `MDMsgWriter`), the resolver returns SELF_REF and leaves the bullet unchanged. Not an error — the LLM included it because it is the page's subject, just no link is needed.

CLI flags:
- `--dry-run` — print unified diff of changes
- `--report` — print resolution report (resolved counts, no-match list, self-ref list, full backlink graph). Combine with `--dry-run` to evaluate impact before writing.

### Step 10: `wiki-implicit-links.py`

**Layer 2** of the entity-linking work. Where `wiki-cross-refs.py` (Layer 1) only touches the explicit `## Cross-references` bullets, this script scans the **synthesized body text** of each `wiki/<slug>.md` page and turns unlinked mentions of known entity names into wiki-page links.

**What gets linked:**
- Single-token page slugs (e.g. `raimd`). Multi-word slugs like `chex-hardware` are skipped — they almost never appear verbatim in body prose.
- Hand-picked PRIMARY_TOKENS (mirrors the dict in `wiki-cross-refs.py`): one-word aliases like `raims`, `chex`, `dyna`, `MDMsgWriter`, `RvMsg`, etc. resolving to specific pages.

Keep `PRIMARY_TOKENS` in sync between the two scripts when adding new aliases. (They're duplicated by repo convention; a shared `wiki_common.py` module would be a nice future refactor.)

**Protection (what does NOT get linked):**
- Existing markdown links and image links
- Inline code spans (`` `…` ``) and fenced code blocks (```` ```…``` ````)
- Heading lines and `> blockquote` lines
- The bold meta line below H1 (matches lines starting with `**` containing ` · `)
- The `## Cross-references` section (Layer 1 owns it)
- The `## Backlinks` section (Layer 1 generates it)
- Everything from the final `---` onward (sources footer)
- Self-references (a token that points back at the same page)

**Case sensitivity heuristic:** tokens with any uppercase letter (e.g. `MDMsgWriter`) are matched case-sensitively. All-lowercase tokens (e.g. `raimd`) are matched case-insensitively, so `Pumpkin` at the start of a sentence still resolves to `multi-machine-setup.md`. The original casing of the matched substring is preserved in the link label.

**First-mention-only:** by default each (page, target) pair gets at most one link. The script also pre-scans existing markdown links on the page — if the page already has any link to a given target slug, no new implicit link is added for that target. This is the idempotence anchor: re-runs are byte-identical no-ops, even though the originally-first body-text mention is now wrapped in a link and would otherwise look "unlinked" on a second pass.

CLI flags:
- `--dry-run` — print unified diff per page; don't write
- `--report` — print per-page link additions summary (which targets gained links, with sample matched substrings)
- `--all` — link every occurrence per (page, target), not just the first. Noisier; useful for review or when you specifically want every mention linked.
- `--only <substring>` — restrict pages by slug substring (e.g. `--only chex` runs against `chex-hardware.md`)

**Order matters:** run after `wiki-cross-refs.py` and before `wiki-link-topics.py`. Layer 1 may add new `## Backlinks` sections that change line numbers, but the layer-2 protections cover both Cross-references and Backlinks regardless, so swapping order is safe in practice — the canonical pipeline order is what's documented above.

### Step 11: `wiki-link-topics.py`

One-pass linker for the editorial sections (`🔖 Topic clusters` and `🪞 About`). Adds links to:
- **Cluster names → wiki entity pages** when `wiki/<slug>.md` exists (`- **Name** — ...` becomes `- **[Name](wiki/<slug>.md)** — ...`). Strips the now-redundant `*(future: ...)*` suffix and any trailing dangling separator.
- `YYYY-MM-DD` dates → `<date>.md` (only if file exists)
- Backticked session uuids (full or 8-char prefix) → `sessions/<full>.md`
- Backticked `MEMORY.md` and `MEMORY.md#<heading>` → `../MEMORY.md[#anchor]`
- Bare `MEMORY.md` outside backticks → `[MEMORY.md](../MEMORY.md)`

**Idempotent:** existing markdown links and fenced code blocks are protected; subsequent runs report `(no changes)`.

CLI flags:
- `--dry-run` — print unified diff to stdout
- `--section topics` / `--section about` / `--section both` (default)

## Editorial workflow

The composer preserves these editorial regions verbatim:
- 🪞 About this index body
- 🔖 Topic clusters body
- Per-date polished daily-log headlines (date-keyed)

To make editorial changes, **edit `memory/index.md` directly with `edit` or `write`**. The next composer run preserves your changes; the next link-topics run adds links to any new dates / sessions / MEMORY.md refs you mentioned.

Common editorial tasks:
- **Polish a new daily-log headline.** Edit the line; composer keeps it. Don't re-run the composer expecting it to incorporate prose changes you describe in conversation.
- **Grow a topic cluster.** Add a bullet in 🔖 Topic clusters mentioning the new daily/session. Then run `wiki-link-topics.py` to add links automatically.
- **Add a new topic cluster.** When you spot 3+ files on a recurring theme, add a cluster bullet.
- **Update About.** When conventions evolve, edit the About section.

After editorial changes, confirm idempotence:
```bash
python3 scripts/wiki-compose.py --check    # exit 0 if up-to-date
python3 scripts/wiki-implicit-links.py     # should report `0 file(s) would change` on second run
python3 scripts/wiki-link-topics.py        # should report (no changes) on second run
```

## Editorial principles

- **One line per file** in the index. If a file deserves more, that's the per-session detail page or `memory/wiki/<topic>.md` (future), not this index.
- **Preserve user-meaningful headlines.** If a daily file has `## Big day for openclaw-src workflow`, that beats anything I'd write.
- **First user prompt of a session is usually a great summary.** Don't paraphrase unless you must.
- **Keep editorial flourishes** — bold markers, parenthetical context, etc.
- **Group by month, sort newest first** for both dailies and sessions.
- **Topic clusters are the wiki's growing edge.** When you spot a topic in 3+ places, list it. The next iteration creates a real `wiki/<topic>.md` page.
- **Don't enumerate auto/system sessions.** They go in the summary paragraph; query the vector index for incident reconstruction.
- **Footnote significant changes** in the cluster you're editing if helpful.

## Privacy & scope

- **Read** from `memory/*.md`, `MEMORY.md`, sessions JSONL, the deleted/reset/checkpoint variants.
- **Write** only to `memory/index.md`, `memory/sessions/<uuid>.md`, `memory/sessions/<uuid>.full.md`, `memory/sessions/<uuid>.tools.md`, `memory/sessions/YYYY-MM.md`, `memory/sessions/.summaries.json`.
- **Do not write** to `MEMORY.md`, daily files, or session transcripts.
- **Do not exfiltrate** session content beyond the summarizer (Anthropic Haiku) and the local memory-index reindex.
- **Redact secrets** if any user prompt accidentally contains them. The cache stores summaries, not raw prompts, so the leakage surface is small.

## Failure modes

- **Empty extraction TSVs** → script failed; check workspace path, sessions dir
- **Date format weirdness** → some sessions have null timestamps; fall back to envelope-derived local date or session record `timestamp` field
- **Massive new content** → if more than 20 new sessions or 5 new daily files since last regen, do a fuller editorial pass on topic clusters too
- **Summarizer 429s** → reduce `--concurrency`; retries are built-in
- **Summarizer auth missing** → must `. ~/.openclaw/env.sh` before step 3 to load `ANTHROPIC_API_KEY`
- **Cache corruption** → delete `memory/sessions/.summaries.json` to force re-summarization (costs ~$0.30 at current rates for ~1300 turns × Haiku)
- **First-prompt fallback** — the extractor classifies sessions into three buckets:
  - `sessions-human.tsv` — first user msg (or any later msg in early-format mode) is a real human prompt with the `[Day...]` envelope or no envelope at all but no auto marker
  - `sessions-subagent.tsv` — first user msg matches `[Subagent Context]` / `Begin. Your assigned task` / `You are running as a subagent`
  - `sessions-auto.tsv` — cron jobs, system events, untrusted-metadata stubs, heartbeats, cron-completion callbacks, NOTIFY_USER pings
  - If a new false-positive class shows up, update the regex in `scripts/wiki-extract.sh` rather than working around it in editorial.
- **Orphaned daily files** (daily exists but no surviving session) → known data loss, see `MEMORY.md#Known Data Loss`. The wiki correctly leaves these unlinked from sessions but the daily-file link still works.
- **Tool-call args showing as empty in `.tools.md`** → toolCall key is `arguments` in OpenClaw, not `input`; the reconstructor handles both. If a new transport uses something else, add a fallback in `wiki-session-reconstruct.py`'s `assistant_tool_calls`.

## Invocation patterns

### Manual (typical)

User says: "regenerate the wiki" / "refresh index.md" → run the workflow inline if it's quick (incremental cache hits), or spawn a subagent if it's a full rebuild.

### Subagent task

```
sessions_spawn(
  task: "Regenerate the wiki following the wiki-maintainer skill at ~/.openclaw/workspace/skills/wiki-maintainer/SKILL.md. Run all 8 pipeline steps + memory reindex. Use cached summaries when available. Report back: file counts, fresh summaries generated, any new topic clusters needed.",
  context: "isolated"
)
```

### Cron (suggested)

Daily run via `cron action=add` with `sessionTarget: isolated`, payload kind `agentTurn`, invoking this skill. Daily cadence catches new sessions while they're still fresh.

### Why isolated, not main session

- **Token budget** — main session carries long conversational context; regenerating the wiki is mechanical
- **Latency** — main session shouldn't pause for memory hygiene
- **Separation of concerns** — wiki maintenance is a focused task

Concurrent edits to wiki files are not a real concern: only the wiki-maintainer writes them, writes are atomic at the OS level, last-writer-wins.

## Output style

When done, report briefly:
- File-count changes (dailies / human sessions / subagent / auto)
- Summaries: cache hits / fresh / failures
- Per-session pages: new files written
- Monthly pages: regenerated counts
- New topic clusters added (if editorial work happened)
- Anything notable that surfaced
- Path to one example file the user can open

Keep it short. Don't quote the whole index back.

## Recent design history (for future maintainers)

These design decisions came from real user feedback during the Apr 2026 buildout — preserving the rationale here so future-you doesn't re-litigate:

- **Why per-session detail pages, not inline turn lists in `index.md`?** The index would balloon to 1000+ lines. Detail pages stay focused; index stays browseable.
- **Why three transcript flavors (`detail` / `full` / `tools`)?** User workflow is opening multiple windows side-by-side: daily for context, detail for q/a summary, full for prose, tools for what was actually run. Each opens in a separate browser tab.
- **Why monthly session pages?** Same reason: keeps `index.md` small even as sessions accumulate over years. `index.md` is the dashboard; monthly pages are the drill-down.
- **Why local date (envelope) instead of UTC date?** Cross-midnight sessions are logically one day's activity. Using UTC date split them into two unrelated-looking rows.
- **Why is the continuation cap 24 (not 4)?** With monthly pages, a sprawling 28-day session distributes across multiple monthly pages, so a higher cap is fine. The cap counts only HUMAN continuation rows; cron-callback dates are filtered out and don't consume slots.
- **Why claude-haiku-4-5 (not local model on dyna)?** 800+ turns × ~2-3s/call sequentially is 30-45 min on local; Haiku at concurrency 8 finishes in ~5 min. Cost is sub-dollar.
- **Why is the link-topics step separate from the composer?** The composer treats editorial regions as opaque text. The link script transforms them in place, idempotently. Splitting keeps each script's responsibility clean and makes the composer's "preserve verbatim" contract simple to reason about.
- **Why is `wiki-entity-pages.py` separate from the cluster-link pass?** Different cost profiles (entity synthesis is LLM-bound, link transformation is regex-bound) and different invocation cadences (entity pages re-synthesize when sources change; link pass runs every regen). Separating them lets the link pass stay cheap while the entity pass can be skipped on quick regens.
- **Why session DETAIL pages (q/a summary) as entity-synthesis input, not full transcripts?** The detail pages are already LLM-distilled; feeding them in keeps prompt sizes manageable and avoids re-summarizing the same content twice. If the synthesis quality ever degrades, switching to `<uuid>.full.md` is a one-line change but burns more tokens.
- **What's NOT yet built (the remaining gap to a full Karpathy wiki):** entity DISCOVERY (LLM proposes new clusters from new content), ENTITY-TO-ENTITY linking (`wiki/raimd.md` should link `wiki/openclaw-src.md` automatically when both exist and one mentions the other), and STALENESS detection ("this page was synthesized from sources up to 2026-04-15; new mentions on 2026-04-26 not yet integrated"). The synthesized pages already include `- related: <name>` lines that an automated pass could resolve.
- **Why does `wiki-session-reconstruct.py` need its own early-format fallback?** The script that builds `.full.md` and `.tools.md` originally only matched `[Day...]`-prefixed user turns, which silently dropped user content from early-format sessions. Symptom: pre-Mar-17 sessions showed assistant prose with no user turns at all. Fix mirrors the dual-mode pattern in `wiki-extract.sh` and `wiki-turns-extract.py`: if no envelope exists in the session, classify by first-line auto markers and fall back to UTC timestamp for display.

## See also

- `MEMORY.md#Known Data Loss` — sessions that pre-date the `.deleted.<timestamp>` rename behavior were genuinely unlinked. Daily files are the durable record.
- `~/.openclaw/workspace/AGENTS.md` — workspace conventions
- `~/.openclaw/workspace/SOUL.md` — Chesswitch's persona / voice
