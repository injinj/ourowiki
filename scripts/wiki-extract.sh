#!/usr/bin/env bash
# wiki-extract.sh — gather structured data for memory/index.md regeneration
#
# Outputs TSV files under /tmp/wiki-build/ that the wiki-maintainer agent
# (or any LLM with this skill) reads to compose the final index.md.
#
# Run: bash ~/.openclaw/workspace/scripts/wiki-extract.sh
# Or:  bash <this-script> --json   for machine-readable JSON output

set -euo pipefail

WORKSPACE="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
SESSIONS_DIR="${OPENCLAW_SESSIONS_DIR:-$HOME/.openclaw/agents/main/sessions}"
OUT_DIR="${WIKI_BUILD_DIR:-/tmp/wiki-build}"
JSON_MODE=false

for arg in "${@:-}"; do
  [ -z "$arg" ] && continue
  case "$arg" in
    --json) JSON_MODE=true ;;
    --out-dir=*) OUT_DIR="${arg#*=}" ;;
    -h|--help)
      cat <<EOF
wiki-extract.sh — extract structured data for memory wiki

Outputs (under \$OUT_DIR, default /tmp/wiki-build):
  dailies.tsv             date \\t headline-list-piped
  sessions-human.tsv      date \\t uuid \\t bytes \\t first-user-message
  sessions-subagent.tsv   date \\t uuid \\t bytes \\t first-user-message
  sessions-auto.tsv       date \\t uuid \\t bytes
  memorymd-sections.txt   long-term MEMORY.md ## section list
  summary.json            (with --json) all of the above as one JSON document

Env:
  OPENCLAW_WORKSPACE     workspace dir (default: \$HOME/.openclaw/workspace)
  OPENCLAW_SESSIONS_DIR  sessions dir   (default: \$HOME/.openclaw/agents/main/sessions)
  WIKI_BUILD_DIR         output dir     (default: /tmp/wiki-build)
EOF
      exit 0
      ;;
  esac
done

mkdir -p "$OUT_DIR"

# ---- Daily files: extract date + H2 headers ----
> "$OUT_DIR/dailies.tsv"
for f in $(ls "$WORKSPACE/memory"/2026-*.md 2>/dev/null | sort); do
  date="$(basename "$f" .md)"
  # grep returns non-zero on no match — neutralize for set -e
  topics="$( { grep -E '^## ' "$f" 2>/dev/null || true; } | sed 's/^## //' | head -10 | tr '\n' '|' | sed 's/|$//')"
  if [ -z "$topics" ]; then
    first="$( { grep -m1 -E '^[A-Za-z]' "$f" 2>/dev/null || true; } | head -c 100)"
    if [ -n "$first" ]; then
      topics="(no headers) $first"
    else
      topics="(empty)"
    fi
  fi
  printf '%s\t%s\n' "$date" "$topics" >> "$OUT_DIR/dailies.tsv"
done

# ---- Sessions: classify human-driven vs subagent vs auto/system ----
# A session is HUMAN if its first user turn:
#   - starts with [Day YYYY-MM-DD HH:MM TZ]
#   - AND the content after that prefix is a real human prompt, not a cron callback or subagent boilerplate
# A session is SUBAGENT if its first user turn matches [Day...] but the content is [Subagent Context] (or similar).
# Everything else (system events, cron jobs, untrusted-metadata stubs, heartbeats) goes to auto.
#
# Long sessions that span multiple distinct calendar dates emit one row per
# date in sessions-human.tsv: the first human user message of THAT date.
# This is what surfaces, e.g., a Perplexity discussion that happened on day 2
# of a session that opened with an unrelated topic on day 1. The earliest
# date's row uses the unprefixed message; later dates carry the marker
# `[continuation]` so editorial passes can identify them.
: > "$OUT_DIR/sessions-human.tsv"
: > "$OUT_DIR/sessions-subagent.tsv"
: > "$OUT_DIR/sessions-auto.tsv"

if [ -d "$SESSIONS_DIR" ]; then
  for uuid in $(ls "$SESSIONS_DIR" 2>/dev/null | sed -E 's/^([0-9a-f-]{36}).*/\1/' | sort -u); do
    # Pick canonical file: prefer the largest available variant (handles cases
    # where the live .jsonl was rotated to .reset.* and only a stub remains).
    # Order of preference: largest among {.jsonl, .reset.*, .deleted.*, .checkpoint.*}.
    file=""
    file_size=0
    for variant in "${uuid}.jsonl" "${uuid}.jsonl.reset."* "${uuid}.jsonl.deleted."* "${uuid}.checkpoint."*.jsonl; do
      for cand in "$SESSIONS_DIR"/$variant; do
        if [ -f "$cand" ]; then
          sz="$(stat -c %s "$cand" 2>/dev/null || echo 0)"
          if [ "$sz" -gt "$file_size" ]; then
            file="$cand"
            file_size="$sz"
          fi
        fi
      done
    done
    [ -z "$file" ] && continue

    # All human-prefixed user messages, paired with their LOCAL date (parsed
    # from the [Day YYYY-MM-DD HH:MM TZ] envelope text rather than the UTC
    # timestamp). This matters because a session that runs across midnight
    # local time is logically one day's activity, not two.
    #
    # We dump ALL text parts of ALL user messages, then filter to those that
    # start with the day-envelope. This matches the original code's behavior
    # of scanning past noise like `Sender (untrusted metadata)` stubs to find
    # the actual human turns.
    #
    # Format: YYYY-MM-DD<TAB>message-text
    human_msgs_by_date="$( { jq -r '
      select(.type=="message" and .message.role=="user")
      | (.message.content // [])
      | map(select(.type=="text") | .text)
      | .[]?' "$file" 2>/dev/null \
      | grep -E '^\[(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [0-9]{4}-[0-9]{2}-[0-9]{2} ' \
      | sed -E 's/^\[(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([0-9]{4}-[0-9]{2}-[0-9]{2}) [^]]*\] *(.*)$/\2\t\3/' \
      || true; } )"

    # Earliest message timestamp (any kind), used as fallback when no human msg exists.
    earliest_date="$( { jq -r 'select(.type=="message" and .timestamp != null) | .timestamp' "$file" 2>/dev/null || true; } | head -1 | cut -dT -f1)"

    bytes="$file_size"

    if [ -z "$human_msgs_by_date" ]; then
      # No envelope-formatted human turns. This may be:
      #   (a) An early-format session (predates the [Day...] envelope convention).
      #       Human turns are raw prose; we have to scan the whole session.
      #   (b) A session that opens with cron/system but the user picked up
      #       the conversation later ("hybrid" sessions, common in early Feb).
      #   (c) A genuinely auto/cron-only session (no human follow-up at all).
      #
      # Strategy: find the FIRST user message that does not match any auto
      # marker. If found, classify the session as human (early-format),
      # using that message as the headline and its UTC date as the row date.
      # If no such message exists, classify as auto.
      # Emit one row per user message text part: <ts>\t<first-line-of-text>.
      # Limiting to the first line ensures we're matching the START of the
      # actual text (which is where auto markers live), not random matching
      # mid-content lines of a cron message body.
      first_human_early="$( { jq -r '
        select(.type=="message" and .message.role=="user")
        | (.timestamp // "") as $ts
        | (.message.content // [])
        | map(select(.type=="text") | .text)
        | .[]?
        | split("\n")[0]
        | select(length > 0)
        | $ts + "\t" + .' "$file" 2>/dev/null \
        | grep -vE $'\t(\\[cron:|System:|\\[Subagent Context\\]|Begin\\. Your assigned task|You are running as a subagent|A cron job "|A subagent task "|The cron job (failed|completed)|NOTIFY_USER:|\\[OpenClaw heartbeat poll\\]|<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>|Sender \\(untrusted metadata\\):)' \
        | head -1 || true; } )"

      if [ -z "$first_human_early" ]; then
        printf '%s\t%s\t%s\n' "$earliest_date" "$uuid" "$bytes" >> "$OUT_DIR/sessions-auto.tsv"
        continue
      fi

      # Split timestamp from text
      early_ts="$(printf '%s' "$first_human_early" | cut -f1)"
      early_text="$(printf '%s' "$first_human_early" | cut -f2-)"
      [ -z "$early_text" ] && {
        printf '%s\t%s\t%s\n' "$earliest_date" "$uuid" "$bytes" >> "$OUT_DIR/sessions-auto.tsv"
        continue
      }
      # Use the user-msg's UTC date if we got a timestamp, else session earliest
      early_date="$(printf '%s' "$early_ts" | cut -dT -f1)"
      [ -z "$early_date" ] && early_date="$earliest_date"

      msg="$(printf '%s' "$early_text" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g' | head -c 200)"
      printf '%s\t%s\t%s\t%s\n' "$early_date" "$uuid" "$bytes" "$msg" >> "$OUT_DIR/sessions-human.tsv"
      continue
    fi

    # Walk distinct dates that have human messages, in chronological order.
    # For each date, emit ONE row with the first human msg of that date.
    # Bucket assignment is based on the FIRST date's first message (so a
    # subagent boilerplate session stays in -subagent for all its rows).
    classify_msg() {
      local m="$1"
      if printf '%s' "$m" | grep -qE '^\[Subagent Context\]|^Begin\. Your assigned task|^You are running as a subagent'; then
        echo subagent
      elif printf '%s' "$m" | grep -qE '^A cron job "[^"]+" just completed|^A subagent task "|^The cron job (failed|completed)|^Cron:|^NOTIFY_USER:'; then
        echo auto
      else
        echo human
      fi
    }

    bucket=""
    is_first_date=true
    rows_emitted=0
    # MAX_CONTINUATION_DATES caps how many continuation rows a single session
    # contributes to the human bucket. Sessions can run for weeks; without a
    # cap one chatty session could swamp a month's page. The cap counts ONLY
    # human-classified continuation rows (cron callbacks etc. are skipped
    # entirely and don't consume a slot). With monthly index pages, a higher
    # cap is fine because rows distribute across multiple month files.
    MAX_CONTINUATION_DATES=24
    # Walk distinct local dates that have human messages, in chronological
    # order. For each date, emit ONE row with the first human msg of that
    # date — BUT skip cron-callback / system messages even on continuation
    # dates (those would otherwise pollute the human bucket).
    while IFS=$'\t' read -r d raw; do
      [ -z "$d" ] && continue
      [ -z "$raw" ] && continue
      msg="$(printf '%s' "$raw" | sed -E 's/^\[[^]]+\] *//' | tr '\n' ' ' | head -c 200)"
      kind="$(classify_msg "$msg")"
      if [ -z "$bucket" ]; then
        bucket="$kind"
      fi
      # Continuation rows must be human-bucket-worthy; skip cron callbacks etc.
      if ! $is_first_date && [ "$kind" != "human" ]; then
        continue
      fi
      # Cap continuation explosion. Counts only HUMAN continuation rows;
      # auto-callback dates are filtered above and don't consume a slot.
      if ! $is_first_date && [ "$rows_emitted" -ge "$MAX_CONTINUATION_DATES" ]; then
        continue
      fi
      if $is_first_date; then
        is_first_date=false
        out_msg="$msg"
      else
        out_msg="[continuation] $msg"
      fi
      case "$bucket" in
        subagent) printf '%s\t%s\t%s\t%s\n' "$d" "$uuid" "$bytes" "$out_msg" >> "$OUT_DIR/sessions-subagent.tsv" ;;
        auto)     # auto rows don't carry message text; only emit one (earliest date)
                  if [ "$out_msg" = "$msg" ]; then
                    printf '%s\t%s\t%s\n' "$d" "$uuid" "$bytes" >> "$OUT_DIR/sessions-auto.tsv"
                  fi ;;
        human)    printf '%s\t%s\t%s\t%s\n' "$d" "$uuid" "$bytes" "$out_msg" >> "$OUT_DIR/sessions-human.tsv" ;;
      esac
      rows_emitted=$((rows_emitted + 1))
    done < <(
      # For each date, pick the first message that is NOT a cron/system
      # callback. Falls back to the first message overall if every message
      # of that date is auto-flagged. The auto-skip in the loop will then
      # discard the date.
      printf '%s\n' "$human_msgs_by_date" | awk -F'\t' '
        BEGIN {
          # Patterns that mark a stripped (post-envelope) message as auto.
          # Mirror classify_msg in shell.
          auto_pat = "^(A cron job \"|A subagent task \"|The cron job (failed|completed)|Cron:|NOTIFY_USER:|\\[Subagent Context\\]|Begin\\. Your assigned task|You are running as a subagent)"
        }
        {
          d = $1
          # Body without the [Day...] envelope (if present).
          body = $2
          sub(/^\[[^]]+\][[:space:]]*/, "", body)
          is_auto = (body ~ auto_pat)
          # Track first-overall and first-human per date.
          if (!(d in first_any)) first_any[d] = $0
          if (!is_auto && !(d in first_human)) first_human[d] = $0
        }
        END {
          # Output dates in chronological order, preferring the human row.
          n = asorti(first_any, ord)
          for (i = 1; i <= n; i++) {
            d = ord[i]
            if (d in first_human) print first_human[d]
            else print first_any[d]
          }
        }
      '
    )
  done
fi

sort -o "$OUT_DIR/sessions-human.tsv"    "$OUT_DIR/sessions-human.tsv"
sort -o "$OUT_DIR/sessions-subagent.tsv" "$OUT_DIR/sessions-subagent.tsv"
sort -o "$OUT_DIR/sessions-auto.tsv"     "$OUT_DIR/sessions-auto.tsv"

# ---- MEMORY.md section list ----
: > "$OUT_DIR/memorymd-sections.txt"
if [ -f "$WORKSPACE/MEMORY.md" ]; then
  { grep -E '^##' "$WORKSPACE/MEMORY.md" || true; } >> "$OUT_DIR/memorymd-sections.txt"
fi

# ---- Counts summary ----
human_count=$(wc -l    < "$OUT_DIR/sessions-human.tsv"    | tr -d ' ')
subagent_count=$(wc -l < "$OUT_DIR/sessions-subagent.tsv" | tr -d ' ')
auto_count=$(wc -l     < "$OUT_DIR/sessions-auto.tsv"     | tr -d ' ')
daily_count=$(wc -l    < "$OUT_DIR/dailies.tsv"           | tr -d ' ')
memmd_count=$(wc -l    < "$OUT_DIR/memorymd-sections.txt" | tr -d ' ')

# ---- Pre-rendered editorial fragments ----
# These are FULL lines/paragraphs the LLM should drop into index.md verbatim.
# Output is dotenv-style with single-quoted values so it's safe to read with
# any parser. Do NOT shell-source; parse line-by-line instead. The single
# quoting also avoids backtick command-substitution if anyone tries to source it.
#
# Schema (one per line, key='value'):
#   COVERAGE_LINE             - bold coverage summary for top-of-file metadata
#   LAST_REGENERATED_LINE     - full "**Last regenerated:** ..." line
#   AUTO_SECTION_PARAGRAPH    - full paragraph for the "Sessions — automated" section
#   FOOTER_LINE               - italicized footer line at bottom of index.md
#   GENERATION_DATE           - YYYY-MM-DD
#   TIMESTAMP_ISO             - full ISO 8601 timestamp
emit_env() {
  local key="$1"
  local val="$2"
  # Escape single quotes by closing/escaping/reopening: ' -> '\''
  local escaped="${val//\'/\'\\\'\'}"
  printf "%s='%s'\n" "$key" "$escaped"
}

gen_date="$(date +%Y-%m-%d)"
gen_iso="$(date -Iseconds)"

coverage="$daily_count daily files · $human_count human-driven session transcripts · $subagent_count subagent task transcripts · $auto_count automated/system transcripts · 1 long-term \`MEMORY.md\`"

last_regen="**Last regenerated:** $gen_date (script-driven by wiki-maintainer skill)"

auto_para="$auto_count transcripts where the first turn is a cron job, system event, or boilerplate inbound metadata. Mostly daily \`workspace-activity-report\` runs of cron \`9ab19c55-33d1-449d-9cb3-4c98853a8bb1\`. Generally low-value individually but useful for incident reconstruction. Not enumerated here — query the vector index by date or topic if needed."

subagent_para=""
if [ "$subagent_count" -gt 0 ]; then
  subagent_para="$subagent_count subagent task transcript(s). The wiki-maintainer regen itself runs as a subagent and shows up in this bucket; expect this count to grow by one each regen."
fi

footer="*Generated $gen_iso by the wiki-maintainer skill (\`scripts/wiki-extract.sh\` + \`skills/wiki-maintainer/SKILL.md\`) · stored in workspace, gitignored alongside daily memory*"

{
  emit_env COVERAGE_LINE              "$coverage"
  emit_env LAST_REGENERATED_LINE      "$last_regen"
  emit_env AUTO_SECTION_PARAGRAPH     "$auto_para"
  emit_env SUBAGENT_SECTION_PARAGRAPH "$subagent_para"
  emit_env FOOTER_LINE                "$footer"
  emit_env GENERATION_DATE            "$gen_date"
  emit_env TIMESTAMP_ISO              "$gen_iso"
} > "$OUT_DIR/summary.env"

# Plain human-readable summary too
{
  echo "Wiki extract summary"
  echo "  Daily files:           $daily_count"
  echo "  Human sessions:        $human_count"
  echo "  Subagent sessions:     $subagent_count"
  echo "  Auto/system sessions:  $auto_count"
  echo "  MEMORY.md sections:    $memmd_count"
  echo "  Generated at:          $gen_iso"
  if [ "$subagent_count" -gt 0 ]; then
    echo ""
    echo "  Note: The current wiki-maintainer regen subagent itself counts toward"
    echo "  the subagent total. Each regen creates +1 subagent session that the"
    echo "  next regen will see."
  fi
} > "$OUT_DIR/summary.txt"

if $JSON_MODE; then
  jq -n \
    --arg date "$(date -Iseconds)" \
    --argjson dailies   "$daily_count" \
    --argjson human     "$human_count" \
    --argjson subagent  "$subagent_count" \
    --argjson auto      "$auto_count" \
    --argjson memmd     "$memmd_count" \
    --arg out "$OUT_DIR" \
    '{generatedAt:$date, counts:{dailies:$dailies, sessionsHuman:$human, sessionsSubagent:$subagent, sessionsAuto:$auto, memoryMdSections:$memmd}, outDir:$out}'
else
  cat "$OUT_DIR/summary.txt"
  echo ""
  echo "Output dir: $OUT_DIR"
fi
