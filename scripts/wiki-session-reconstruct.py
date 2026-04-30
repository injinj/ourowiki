#!/usr/bin/env python3
"""
wiki-session-reconstruct.py — reconstruct a session as readable markdown.

Modes:
  --mode prose   (default) only user prompts + assistant prose text. Skips
                 tool calls and tool results. Good for re-reading a
                 conversation as a transcript.
  --mode tools   user prompts + assistant prose + tool calls (compact, with
                 args summarized) + tool results (truncated). Reads close
                 to the live TUI experience.
  --mode raw     full structured dump: every message record, every content
                 part, full text. Big files but loss-less.

Usage:
  python3 wiki-session-reconstruct.py <uuid>                  # prose mode, prints to stdout
  python3 wiki-session-reconstruct.py <uuid> --mode tools
  python3 wiki-session-reconstruct.py <uuid> --mode raw -o /tmp/full.md
  python3 wiki-session-reconstruct.py <uuid> -o ~/.openclaw/workspace/memory/sessions/<uuid>.full.md

Source files: same canonical-pick logic as the rest of the wiki pipeline
(largest of .jsonl / .reset.* / .deleted.* / .checkpoint.*).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SESSIONS_DIR = Path(os.environ.get(
    "OPENCLAW_SESSIONS_DIR",
    str(Path.home() / ".openclaw" / "agents" / "main" / "sessions")
))

ENVELOPE_RE = re.compile(
    r"^\[(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) ([A-Z]+)\]\s*",
    re.MULTILINE,
)

# Early-format auto markers: if a user message's first line begins with any
# of these, it's a system event / cron callback / boilerplate, not a human
# turn. Used for sessions predating the [Day...] envelope convention.
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


def pick_canonical_file(uuid: str) -> Path | None:
    candidates = [SESSIONS_DIR / f"{uuid}.jsonl"]
    for pattern in (f"{uuid}.jsonl.reset.*", f"{uuid}.jsonl.deleted.*", f"{uuid}.checkpoint.*.jsonl"):
        candidates.extend(SESSIONS_DIR.glob(pattern))
    candidates = [c for c in candidates if c.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def parse_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def find_user_envelope(content) -> tuple[str, str] | None:
    """Return (local_ts_str, prompt_body) or None if no envelope found."""
    if not isinstance(content, list):
        return None
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        t = part.get("text", "")
        if not isinstance(t, str):
            continue
        last_match = None
        for m in ENVELOPE_RE.finditer(t):
            last_match = m
        if last_match is None:
            continue
        local = f"{last_match.group(2)} {last_match.group(3)} {last_match.group(4)}"
        body = t[last_match.end():].strip()
        return (local, body)
    return None


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
    """Convert ISO UTC timestamp to a 'YYYY-MM-DD HH:MM UTC' display string
    for early-format sessions that have no [Day...] envelope."""
    if not ts_iso:
        return ""
    try:
        date_part, time_part = ts_iso.split("T", 1)
        hhmm = time_part[:5]
        return f"{date_part} {hhmm} UTC"
    except (ValueError, IndexError):
        return ""


def find_user_early_format(content) -> tuple[str, str] | None:
    """Early-format fallback: pick the first text part whose first line
    is NOT an auto marker. Returns (raw_first_line_or_'', full_text).
    Caller supplies the timestamp from the message envelope.
    """
    if not isinstance(content, list):
        return None
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        t = part.get("text", "")
        if not isinstance(t, str) or not t.strip():
            continue
        first_line = t.lstrip().splitlines()[0]
        if AUTO_FIRST_LINE_RE.match(first_line):
            return None
        return (first_line, t.strip())
    return None


def assistant_text_parts(content) -> list[str]:
    """Return the text parts of an assistant message (skipping toolCall, etc)."""
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            t = part.get("text", "")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
    return out


def assistant_tool_calls(content) -> list[dict]:
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "toolCall":
            # OpenClaw's runtime stores call args under "arguments". Some
            # provider transports also use "input" / "params". Try in order.
            args = part.get("arguments")
            if args is None:
                args = part.get("input")
            if args is None:
                args = part.get("params", {})
            out.append({
                "name": part.get("name", "?"),
                "input": args if isinstance(args, dict) else {"_raw": args},
                "id": part.get("id", ""),
            })
    return out


def tool_result_text(content, max_chars: int) -> str:
    if not isinstance(content, list):
        return ""
    out_parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            t = part.get("text", "")
            if isinstance(t, str):
                out_parts.append(t)
        elif part.get("type") == "toolResult":
            # nested results
            inner = part.get("content")
            if isinstance(inner, list):
                for ip in inner:
                    if isinstance(ip, dict) and ip.get("type") == "text":
                        ti = ip.get("text", "")
                        if isinstance(ti, str):
                            out_parts.append(ti)
            elif isinstance(inner, str):
                out_parts.append(inner)
    text = "\n".join(out_parts).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n\n_…[truncated {len(text) - max_chars} chars]…_"
    return text


def fmt_tool_input(inp, max_chars: int = 400) -> str:
    """Compact one-line representation of tool input args."""
    if not isinstance(inp, dict):
        s = json.dumps(inp, ensure_ascii=False)
    else:
        # Show key=value pairs, truncating values
        parts = []
        for k, v in inp.items():
            if isinstance(v, str):
                vs = v.replace("\n", "\\n")
                if len(vs) > 80:
                    vs = vs[:77] + "…"
                parts.append(f"{k}={json.dumps(vs)}")
            else:
                vs = json.dumps(v, ensure_ascii=False)
                if len(vs) > 80:
                    vs = vs[:77] + "…"
                parts.append(f"{k}={vs}")
        s = ", ".join(parts)
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


def render(uuid: str, mode: str, max_tool_result_chars: int) -> str:
    path = pick_canonical_file(uuid)
    if path is None:
        return f"# Session `{uuid}`\n\n_(no session file found)_\n"

    lines: list[str] = []
    lines.append(f"# Session `{uuid[:8]}` — {mode} reconstruction")
    lines.append("")
    lines.append(f"**UUID:** `{uuid}`  ")
    lines.append(f"**Source file:** `{path.name}`  ")
    lines.append(f"**Mode:** `{mode}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    user_count = 0
    asst_count = 0
    tool_count = 0
    last_local_date = ""

    # Build a fast lookup from message id → record so toolResult children can resolve which call they answer
    if mode == "tools":
        records = list(parse_jsonl(path))
        # toolResult records reference the toolCall id; we render them after their assistant turn
    else:
        records = parse_jsonl(path)

    if mode == "raw":
        for obj in records:
            lines.append("```json")
            lines.append(json.dumps(obj, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
        return "\n".join(lines)

    # prose / tools modes share most logic
    records = list(records)
    use_early_format = not session_has_envelope(records)

    for obj in records:
        if obj.get("type") != "message":
            continue
        msg = obj.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            local: str
            body: str
            if not use_early_format:
                env = find_user_envelope(content)
                if env is None:
                    continue
                local, body = env
            else:
                early = find_user_early_format(content)
                if early is None:
                    continue
                _first_line, body = early
                ts_iso = obj.get("timestamp") or ""
                local = iso_to_local_str(ts_iso)
                if not local:
                    continue
            if not body.strip():
                continue
            local_date = local.split(" ", 1)[0]
            if local_date != last_local_date:
                lines.append(f"## {local_date}")
                lines.append("")
                last_local_date = local_date
            user_count += 1
            time_part = local.split(" ", 1)[1].rsplit(" ", 1)[0]
            lines.append(f"### 👤 user — {time_part}")
            lines.append("")
            lines.append(body)
            lines.append("")

        elif role == "assistant":
            texts = assistant_text_parts(content)
            calls = assistant_tool_calls(content)
            if not texts and not calls:
                continue
            asst_count += 1
            if texts:
                lines.append(f"### 🤖 assistant")
                lines.append("")
                for t in texts:
                    lines.append(t)
                    lines.append("")
            if mode == "tools" and calls:
                if not texts:
                    lines.append(f"### 🤖 assistant")
                    lines.append("")
                for c in calls:
                    lines.append(f"<details><summary>🔧 <code>{c['name']}</code></summary>")
                    lines.append("")
                    lines.append("```")
                    lines.append(fmt_tool_input(c["input"], max_chars=2000))
                    lines.append("```")
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")

        elif role == "toolResult" and mode == "tools":
            tr_text = tool_result_text(content, max_tool_result_chars)
            if not tr_text:
                continue
            tool_count += 1
            lines.append("<details><summary>📤 tool result</summary>")
            lines.append("")
            lines.append("```")
            lines.append(tr_text)
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    lines.append("---")
    lines.append("")
    summary = f"_user turns: {user_count} · assistant turns: {asst_count}"
    if mode == "tools":
        summary += f" · tool results: {tool_count}"
    summary += "_"
    lines.append(summary)

    return "\n".join(lines).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uuid", help="Session UUID (or first 8 chars)")
    ap.add_argument("--mode", choices=["prose", "tools", "raw"], default="prose")
    ap.add_argument("-o", "--output", help="Write to file (default: stdout)")
    ap.add_argument("--tool-result-chars", type=int, default=2000,
                    help="Truncate each tool result to this many chars in 'tools' mode")
    args = ap.parse_args()

    # Allow short UUID prefix
    uuid = args.uuid
    if len(uuid) < 36:
        candidates = list(SESSIONS_DIR.glob(f"{uuid}*"))
        if not candidates:
            print(f"no session matching {uuid}", file=sys.stderr)
            sys.exit(1)
        # Extract full uuid from first match's name
        first = candidates[0].name
        m = re.match(r"^([0-9a-f-]{36})", first)
        if not m:
            print(f"could not extract uuid from {first}", file=sys.stderr)
            sys.exit(1)
        uuid = m.group(1)

    body = render(uuid, args.mode, args.tool_result_chars)
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        print(f"wrote {out} ({len(body)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(body)


if __name__ == "__main__":
    main()
