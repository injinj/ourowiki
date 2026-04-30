#!/usr/bin/env python3
"""
wiki-turns-extract.py — pull per-turn (user_request, assistant_response) pairs
from each human-bucket session, ready for summarization.

Reads:  /tmp/wiki-build/sessions-human.tsv   (produced by wiki-extract.sh)
Writes: /tmp/wiki-build/turns/<uuid>.jsonl   (one JSON object per turn)

Each output line is:
  {
    "uuid": "<session-uuid>",
    "msg_id": "<assistant message id>",       # stable cache key
    "ts_user_iso": "2026-04-29T07:30:27Z",    # UTC
    "ts_user_local": "2026-04-29 00:30 PDT",  # parsed from envelope
    "user_text": "is there a perplexity model option?",  # envelope stripped
    "assistant_text": "<full text, truncated>",
    "assistant_text_len": 12345
  }

Notes:
- The cache key is `<uuid>:<msg_id>` where msg_id is the assistant message's id
  (stable across re-extractions). Falls back to user-message id if no
  assistant id available.
- Skips turns where the user message is a system event, cron callback, or
  subagent boilerplate (those don't deserve a summary line).
- Skips turns where the assistant message is empty / error fallback.
- Truncates assistant text to ~4000 chars to keep summarizer prompts cheap.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw" / "workspace")))
SESSIONS_DIR = Path(os.environ.get("OPENCLAW_SESSIONS_DIR", str(Path.home() / ".openclaw" / "agents" / "main" / "sessions")))
OUT_DIR = Path(os.environ.get("WIKI_BUILD_DIR", "/tmp/wiki-build"))
TURNS_DIR = OUT_DIR / "turns"
TURNS_DIR.mkdir(parents=True, exist_ok=True)

ASSISTANT_TRUNCATE = 4000  # chars

# Envelope can appear at the very start of the text OR on a later line
# (after a metadata block like "Sender (untrusted metadata): {...}\n\n[Wed ...] ...").
# Multiline mode + line-anchored. We find_iter and take the LAST occurrence so
# the prompt body is everything after the most recent envelope (in case the
# wrapper text itself contains a fake envelope).
# Envelope can appear at the very start of the text OR on a later line
# (after a metadata block like "Sender (untrusted metadata): {...}\n\n[Wed ...] ...").
ENVELOPE_RE = re.compile(
    r"^\[(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) ([A-Z]+)\]\s*",
    re.MULTILINE,
)

# First-line auto markers: if a user message's first line begins with any of
# these, it's a system event / cron callback / boilerplate, not a human turn.
# Used in EARLY-FORMAT mode (sessions predating the [Day...] envelope), where
# we have to inspect message content directly to tell human apart from auto.
AUTO_FIRST_LINE_RE = re.compile(
    r"^("
    r"\[cron:|"
    r"System:|"
    r"\[Subagent Context\]|"
    r"Begin\. Your assigned task|"
    r"You are running as a subagent|"
    r"A cron job \"|"
    r"A subagent task \"|"
    r"The cron job (failed|completed)|"
    r"NOTIFY_USER:|"
    r"\[OpenClaw heartbeat poll\]|"
    r"<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>|"
    r"Sender \(untrusted metadata\):|"
    r"\[message_id:"
    r")"
)
SKIP_USER_RE = re.compile(
    r"^(\[Subagent Context\]|Begin\. Your assigned task|You are running as a subagent|"
    r"A cron job \"|A subagent task \"|The cron job (failed|completed)|Cron:|NOTIFY_USER:|"
    r"<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>|\[OpenClaw heartbeat poll\])"
)
ASSISTANT_SKIP_RE = re.compile(
    r"^(NO_REPLY\s*$|⚠️ Agent failed|HEARTBEAT_OK\s*$|\[assistant turn failed)"
)


def pick_canonical_file(uuid: str) -> Path | None:
    """Pick the largest available variant for this session UUID."""
    candidates: list[Path] = []
    candidates.append(SESSIONS_DIR / f"{uuid}.jsonl")
    for pattern in (f"{uuid}.jsonl.reset.*", f"{uuid}.jsonl.deleted.*", f"{uuid}.checkpoint.*.jsonl"):
        candidates.extend(SESSIONS_DIR.glob(pattern))
    candidates = [c for c in candidates if c.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def extract_text_parts(content) -> str:
    """Concatenate text parts from a message.content list."""
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text", "")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts).strip()


def parse_jsonl(path: Path):
    """Yield parsed JSON objects line-by-line; skip malformed lines."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def session_has_envelope(records: list[dict]) -> bool:
    """Return True if any user message in the session has a [Day...] envelope."""
    for obj in records:
        if obj.get("type") != "message":
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "")
                if isinstance(t, str) and ENVELOPE_RE.search(t):
                    return True
    return False


def iso_to_local_str(ts_iso: str) -> str:
    """Convert ISO UTC timestamp to a 'YYYY-MM-DD HH:MM UTC' display string.
    Used for early-format sessions that have no [Day...] envelope."""
    if not ts_iso:
        return ""
    # "2026-02-01T03:58:12.345Z" -> "2026-02-01 03:58 UTC"
    try:
        date_part, time_part = ts_iso.split("T", 1)
        hhmm = time_part[:5]
        return f"{date_part} {hhmm} UTC"
    except (ValueError, IndexError):
        return ""


def extract_turns(uuid: str) -> int:
    """Walk the session, pair user→next-assistant turns, write JSONL.

    Two modes:
      Modern: user turns must carry the [Day YYYY-MM-DD HH:MM TZ] envelope.
      Early-format fallback: if the session has NO envelope-prefixed turns
        anywhere, accept any user message whose first line doesn't match an
        auto marker. Date/time come from the message's UTC timestamp.

    Returns count of turns written.
    """
    file = pick_canonical_file(uuid)
    if file is None:
        return 0

    records = list(parse_jsonl(file))
    use_early_format = not session_has_envelope(records)

    out_path = TURNS_DIR / f"{uuid}.jsonl"
    count = 0
    pending_user = None  # (text, ts_iso, ts_local, user_msg_id)

    with out_path.open("w", encoding="utf-8") as out:
        for obj in records:
            if obj.get("type") != "message":
                continue
            msg = obj.get("message") or {}
            role = msg.get("role")
            text = extract_text_parts(msg.get("content"))
            ts_iso = obj.get("timestamp") or ""
            msg_id = obj.get("id") or ""

            if role == "user":
                if not text:
                    continue

                if not use_early_format:
                    # Modern mode: require [Day...] envelope.
                    envelope_match = None
                    envelope_text = None
                    if isinstance(msg.get("content"), list):
                        for part in msg["content"]:
                            if isinstance(part, dict) and part.get("type") == "text":
                                t = part.get("text", "")
                                if isinstance(t, str):
                                    last = None
                                    for mm in ENVELOPE_RE.finditer(t):
                                        last = mm
                                    if last is not None:
                                        envelope_match = last
                                        envelope_text = t
                    if envelope_match is None or envelope_text is None:
                        continue
                    m = envelope_match
                    local_date = m.group(2)
                    local_time = m.group(3)
                    local_tz = m.group(4)
                    stripped = envelope_text[m.end():].strip()
                    if SKIP_USER_RE.match(stripped):
                        pending_user = None
                        continue
                    if not stripped:
                        pending_user = None
                        continue
                    pending_user = {
                        "text": stripped,
                        "ts_iso": ts_iso,
                        "ts_local": f"{local_date} {local_time} {local_tz}",
                        "user_msg_id": msg_id,
                    }
                else:
                    # Early-format mode: classify by first-line content.
                    # Use the first non-empty text part. Auto markers reject;
                    # everything else is treated as a human turn.
                    raw_text = None
                    if isinstance(msg.get("content"), list):
                        for part in msg["content"]:
                            if isinstance(part, dict) and part.get("type") == "text":
                                t = part.get("text", "")
                                if isinstance(t, str) and t.strip():
                                    raw_text = t
                                    break
                    if raw_text is None:
                        continue
                    first_line = raw_text.lstrip().splitlines()[0] if raw_text.strip() else ""
                    if not first_line or AUTO_FIRST_LINE_RE.match(first_line):
                        pending_user = None
                        continue
                    pending_user = {
                        "text": raw_text.strip(),
                        "ts_iso": ts_iso,
                        "ts_local": iso_to_local_str(ts_iso),
                        "user_msg_id": msg_id,
                    }

            elif role == "assistant":
                if pending_user is None:
                    continue
                if not text:
                    continue
                if ASSISTANT_SKIP_RE.match(text):
                    pending_user = None
                    continue
                truncated = text[:ASSISTANT_TRUNCATE]
                record = {
                    "uuid": uuid,
                    "msg_id": msg_id or pending_user["user_msg_id"],
                    "ts_user_iso": pending_user["ts_iso"],
                    "ts_user_local": pending_user["ts_local"],
                    "user_text": pending_user["text"],
                    "assistant_text": truncated,
                    "assistant_text_len": len(text),
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
                pending_user = None

    if count == 0:
        out_path.unlink(missing_ok=True)
    return count


def main():
    tsv = OUT_DIR / "sessions-human.tsv"
    if not tsv.exists():
        print(f"missing {tsv} — run wiki-extract.sh first", file=sys.stderr)
        sys.exit(1)

    uuids = set()
    for line in tsv.read_text().splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) >= 2:
            uuids.add(cols[1])

    total_turns = 0
    sessions_with_turns = 0
    for uuid in sorted(uuids):
        n = extract_turns(uuid)
        if n > 0:
            sessions_with_turns += 1
            total_turns += n
            print(f"  {uuid}  {n:4d} turns")

    print(f"\nWrote {total_turns} turns across {sessions_with_turns} sessions to {TURNS_DIR}/")


if __name__ == "__main__":
    main()
