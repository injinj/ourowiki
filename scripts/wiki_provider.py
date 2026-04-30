"""
wiki_provider.py — small LLM provider shim shared by `wiki-turns-summarize.py`
and `wiki-entity-pages.py`.

Goal: let either script run against Anthropic's Messages API (the original
default) or any OpenAI-compatible Chat Completions endpoint (real OpenAI,
OpenAI-compat servers like llama-server, Ollama, OpenRouter, LiteLLM, etc.)
without baking the routing into the call sites.

Selection is driven by environment variables (mirrors `llm-wiki-compiler`'s
convention so a user moving between tools doesn't have to relearn it):

  OUROWIKI_PROVIDER     "anthropic" (default) | "openai" | "openai-compat"
  OUROWIKI_MODEL        Override model id; falls back to per-script default
                        when unset.

  Anthropic provider:
    ANTHROPIC_API_KEY   required
    ANTHROPIC_BASE_URL  optional; defaults to https://api.anthropic.com

  OpenAI / openai-compat provider:
    OPENAI_API_KEY      required (any non-empty string is fine for local
                        servers that don't actually authenticate)
    OPENAI_BASE_URL     optional; defaults to https://api.openai.com/v1
                        Set this to e.g. http://localhost:8080/v1 for a
                        local llama-server, or to OpenRouter's URL, etc.

The two providers expose the same call signature:

    text, usage = await provider.call(client, system, user,
                                      max_tokens=..., timeout=...)

`usage` is a free-form dict — Anthropic returns {input_tokens, output_tokens};
OpenAI returns {prompt_tokens, completion_tokens, total_tokens}. Callers
that care normalize themselves.

This module is intentionally tiny — a few hundred lines max. It does NOT
try to be a generic LLM client library. It handles two API shapes, retries
on 429/5xx with exponential backoff, and gets out of the way.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Optional

try:
    import httpx
except ImportError:
    print("missing httpx: pip install --user httpx", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "anthropic"

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-5-mini"

# Status codes worth retrying with backoff.
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# Reasoning-model families (gpt-5-*, o1-*, o3-*, gpt-4o-reasoning, etc.)
# consume a large fraction of `max_completion_tokens` on hidden reasoning
# tokens before producing visible content. If the caller asks for, say,
# 80 tokens (fine for haiku) the reasoning will eat all 80 and content
# will be empty (finish_reason=length).
#
# Heuristic: when we detect a reasoning-class model, bump the per-call
# token budget to at least REASONING_MIN_BUDGET. Callers that explicitly
# request more keep the larger value.
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
REASONING_MIN_BUDGET = 4096


def _is_reasoning_model(model_id: str) -> bool:
    m = (model_id or "").strip().lower()
    return any(m.startswith(p) for p in _REASONING_MODEL_PREFIXES)


# ---------------------------------------------------------------------------
# Provider data class
# ---------------------------------------------------------------------------


@dataclass
class Provider:
    """Resolved provider config. Construct with `from_env()`."""
    name: str            # "anthropic" | "openai" | "openai-compat"
    model: str           # resolved model id
    api_key: str
    base_url: str        # without trailing slash

    @classmethod
    def from_env(cls,
                 default_anthropic_model: str = ANTHROPIC_DEFAULT_MODEL,
                 default_openai_model: str = OPENAI_DEFAULT_MODEL,
                 model_override: Optional[str] = None) -> "Provider":
        """Read env vars and return a resolved Provider. Exits on missing
        required values with a friendly diagnostic.

        `model_override` (e.g. from a --model CLI flag) wins over
        OUROWIKI_MODEL which wins over the per-provider default.
        """
        provider = (os.environ.get("OUROWIKI_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
        env_model = (os.environ.get("OUROWIKI_MODEL") or "").strip() or None

        if provider == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                _fatal(
                    "OUROWIKI_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.\n"
                    "  Either export ANTHROPIC_API_KEY=... or switch provider with\n"
                    "  OUROWIKI_PROVIDER=openai (with OPENAI_API_KEY)."
                )
            base = (os.environ.get("ANTHROPIC_BASE_URL") or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
            model = model_override or env_model or default_anthropic_model
            return cls(name="anthropic", model=model, api_key=api_key, base_url=base)

        if provider in ("openai", "openai-compat"):
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                _fatal(
                    f"OUROWIKI_PROVIDER={provider} but OPENAI_API_KEY is not set.\n"
                    "  For local OpenAI-compatible servers any non-empty string works\n"
                    "  (e.g. export OPENAI_API_KEY=local-no-auth)."
                )
            base = (os.environ.get("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL).rstrip("/")
            model = model_override or env_model or default_openai_model
            return cls(name=provider, model=model, api_key=api_key, base_url=base)

        _fatal(
            f"Unknown OUROWIKI_PROVIDER={provider!r}. "
            "Use 'anthropic', 'openai', or 'openai-compat'."
        )

    # ---- HTTP call -----------------------------------------------------

    async def call(self, client: httpx.AsyncClient,
                   system: str, user: str,
                   max_tokens: int = 1024,
                   timeout: float = 60.0,
                   retries: int = 3) -> tuple[str, dict]:
        """Send a single (system, user) prompt and return (text, usage).

        On non-200 / network error after retries, returns ("", {}) and
        prints a one-line diagnostic to stderr. Callers handle empty
        responses gracefully (they already do for the original Anthropic-
        only path).
        """
        if self.name == "anthropic":
            return await self._call_anthropic(client, system, user, max_tokens, timeout, retries)
        # openai + openai-compat go through the same Chat Completions shape
        return await self._call_openai(client, system, user, max_tokens, timeout, retries)

    # ---- Anthropic Messages API ---------------------------------------

    async def _call_anthropic(self, client: httpx.AsyncClient,
                              system: str, user: str,
                              max_tokens: int, timeout: float,
                              retries: int) -> tuple[str, dict]:
        url = f"{self.base_url}/v1/messages"
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        last_err = None
        for attempt in range(retries):
            try:
                r = await client.post(url, json=body, headers=headers, timeout=timeout)
                if r.status_code == 200:
                    data = r.json()
                    text = ""
                    for c in data.get("content") or []:
                        if c.get("type") == "text":
                            text = (c.get("text") or "").strip()
                            break
                    return text, (data.get("usage") or {})
                if r.status_code in _RETRYABLE_STATUS:
                    await asyncio.sleep(2 ** attempt)
                    continue
                last_err = f"http {r.status_code}: {r.text[:300]}"
                break
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 ** attempt)
        print(f"  ! anthropic call failed: {last_err}", file=sys.stderr)
        return "", {}

    # ---- OpenAI Chat Completions API ----------------------------------

    async def _call_openai(self, client: httpx.AsyncClient,
                           system: str, user: str,
                           max_tokens: int, timeout: float,
                           retries: int) -> tuple[str, dict]:
        url = f"{self.base_url}/chat/completions"

        # Reasoning models (gpt-5-*, o1-*, o3-*, o4-*) burn a large fraction
        # of max_completion_tokens on hidden reasoning before producing any
        # visible content. If the caller asks for 80 tokens, the response is
        # often empty with finish_reason="length". Bump the budget.
        effective = max_tokens
        if self.name == "openai" and _is_reasoning_model(self.model):
            effective = max(max_tokens, REASONING_MIN_BUDGET)

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # gpt-5 family uses "max_completion_tokens"; legacy chat models
            # use "max_tokens". The provider type tells us which dialect to
            # speak: provider=openai → modern field; openai-compat → legacy.
            **({"max_completion_tokens": effective} if self.name == "openai"
               else {"max_tokens": effective}),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(retries):
            try:
                r = await client.post(url, json=body, headers=headers, timeout=timeout)
                if r.status_code == 200:
                    data = r.json()
                    text = ""
                    choices = data.get("choices") or []
                    if choices:
                        message = choices[0].get("message") or {}
                        text = (message.get("content") or "").strip()
                    return text, (data.get("usage") or {})
                if r.status_code in _RETRYABLE_STATUS:
                    await asyncio.sleep(2 ** attempt)
                    continue
                last_err = f"http {r.status_code}: {r.text[:300]}"
                break
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 ** attempt)
        print(f"  ! {self.name} call failed: {last_err}", file=sys.stderr)
        return "", {}

    # ---- Diagnostics --------------------------------------------------

    def describe(self) -> str:
        """One-line summary for startup logs."""
        return f"provider={self.name} model={self.model} base={self.base_url}"


def _fatal(msg: str) -> None:
    print(f"wiki_provider: {msg}", file=sys.stderr)
    sys.exit(2)
