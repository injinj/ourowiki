#!/usr/bin/env python3
"""
wiki-turns-summarize.py — generate one-line LLM summaries for every (request,
response) pair, with persistent caching keyed by `<uuid>:<msg_id>`.

Reads:  /tmp/wiki-build/turns/<uuid>.jsonl
Writes: ~/.openclaw/workspace/memory/sessions/.summaries.json   (cache)
        /tmp/wiki-build/turns/<uuid>.summarized.jsonl           (turns + summary)

Concurrency: configurable parallel workers (default 8). Re-runs are cheap
because cached entries are reused.

Auth: provider routing follows OUROWIKI_PROVIDER (anthropic | openai |
openai-compat). Defaults to anthropic with ANTHROPIC_API_KEY. See
wiki_provider.py for the full env-var contract.

Usage:
  python3 wiki-turns-summarize.py [--model <id>] [--concurrency 8]
                                   [--limit-per-session N]   # smoke test
                                   [--only-uuid <uuid>]      # one session

  # Anthropic (default):
  ANTHROPIC_API_KEY=... python3 wiki-turns-summarize.py

  # OpenAI:
  OUROWIKI_PROVIDER=openai OPENAI_API_KEY=... python3 wiki-turns-summarize.py

  # Local llama-server / Ollama / OpenRouter / etc.:
  OUROWIKI_PROVIDER=openai-compat OPENAI_API_KEY=local \
  OPENAI_BASE_URL=http://localhost:8080/v1 \
  OUROWIKI_MODEL=qwen2.5-coder-32b \
      python3 wiki-turns-summarize.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("missing httpx: pip install httpx (or use uv)", file=sys.stderr)
    sys.exit(2)

# Provider shim — handles Anthropic vs OpenAI-compatible routing.
sys.path.insert(0, str(Path(__file__).parent))
from wiki_provider import Provider  # noqa: E402

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace")))
TURNS_DIR = Path(os.environ.get("WIKI_BUILD_DIR", "/tmp/wiki-build")) / "turns"
SESSIONS_OUT_DIR = WORKSPACE / "memory" / "sessions"
SESSIONS_OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = SESSIONS_OUT_DIR / ".summaries.json"

SYSTEM_PROMPT = (
    "You write tight one-line summaries of an assistant's response to a user request. "
    "Goal: future-self at a glance — what did the assistant DO or CONCLUDE? "
    "Constraints: \u2264 22 words, no emoji, no markdown, no leading verb required. "
    "Drop hedge words. Output ONLY the summary line, no quotes, no prefix."
)

USER_TEMPLATE = (
    "USER REQUEST:\n{user}\n\nASSISTANT RESPONSE:\n{assistant}\n\n"
    "One-line summary of the assistant's response (\u226422 words):"
)


def load_cache() -> dict[str, str]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict[str, str]) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE_PATH)


async def call_summary(client: httpx.AsyncClient, provider: Provider,
                       user_text: str, assistant_text: str) -> str:
    user = USER_TEMPLATE.format(user=user_text, assistant=assistant_text)
    text, _usage = await provider.call(
        client, SYSTEM_PROMPT, user, max_tokens=80, timeout=60.0,
    )
    if not text:
        return ""
    return text.strip().splitlines()[0][:300]


async def summarize_session(uuid: str, turns: list[dict], cache: dict[str, str],
                            client: httpx.AsyncClient, provider: Provider,
                            sem: asyncio.Semaphore, save_every: int,
                            counters: dict[str, int]) -> list[dict]:
    """Summarize all turns of one session; return enriched list."""
    enriched = []
    for turn in turns:
        cache_key = f"{uuid}:{turn['msg_id']}"
        if cache_key in cache and cache[cache_key]:
            turn["summary"] = cache[cache_key]
            counters["cache_hits"] += 1
        else:
            async with sem:
                summary = await call_summary(
                    client, provider, turn["user_text"], turn["assistant_text"]
                )
            if summary:
                cache[cache_key] = summary
                turn["summary"] = summary
                counters["fresh"] += 1
                # Periodic save so we don't lose progress on crash
                if counters["fresh"] % save_every == 0:
                    save_cache(cache)
            else:
                turn["summary"] = ""
                counters["failures"] += 1
        enriched.append(turn)
    return enriched


async def main_async(args):
    provider = Provider.from_env(
        default_anthropic_model="claude-haiku-4-5",
        default_openai_model="gpt-5-mini",
        model_override=(args.model or None),
    )
    print(f"  {provider.describe()}", file=sys.stderr)

    if not TURNS_DIR.exists():
        print(f"missing {TURNS_DIR} — run wiki-turns-extract.py first", file=sys.stderr)
        sys.exit(2)

    files = sorted(TURNS_DIR.glob("*.jsonl"))
    files = [f for f in files if not f.name.endswith(".summarized.jsonl")]
    if args.only_uuid:
        files = [f for f in files if f.stem == args.only_uuid]

    cache = load_cache()
    counters = {"cache_hits": 0, "fresh": 0, "failures": 0}
    sem = asyncio.Semaphore(args.concurrency)

    total_turns = 0
    async with httpx.AsyncClient(http2=False) as client:
        tasks = []
        for f in files:
            turns = []
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    turns.append(json.loads(line))
            if args.limit_per_session:
                turns = turns[: args.limit_per_session]
            total_turns += len(turns)
            tasks.append((f.stem, turns, summarize_session(
                f.stem, turns, cache, client, provider, sem,
                args.save_every, counters
            )))

        print(f"summarizing {total_turns} turns across {len(tasks)} sessions "
              f"(concurrency={args.concurrency}, model={provider.model})...")
        t0 = time.time()
        results = await asyncio.gather(*[t[2] for t in tasks])
        elapsed = time.time() - t0

    # Write per-session enriched JSONL
    for (uuid, _, _), enriched in zip(tasks, results):
        out_path = TURNS_DIR / f"{uuid}.summarized.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            for turn in enriched:
                fh.write(json.dumps(turn, ensure_ascii=False) + "\n")

    # Final cache save
    save_cache(cache)

    print(f"\nDone in {elapsed:.1f}s")
    print(f"  cache hits: {counters['cache_hits']}")
    print(f"  fresh:      {counters['fresh']}")
    print(f"  failures:   {counters['failures']}")
    print(f"Cache: {CACHE_PATH} ({len(cache)} entries)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="",
                    help="Model id override; takes precedence over OUROWIKI_MODEL.")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--save-every", type=int, default=20, help="Save cache every N fresh summaries")
    ap.add_argument("--limit-per-session", type=int, default=0, help="Smoke test: max turns per session (0=all)")
    ap.add_argument("--only-uuid", default="", help="Only process this session UUID")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
