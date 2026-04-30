#!/usr/bin/env bash
# install.sh — set up ourowiki against an OpenClaw workspace.
#
# Writes ~/.ourowiki/env (or $OUROWIKI_ENV_FILE) with the provider config,
# optionally registers a shell-cron entry, and runs the pipeline once
# end-to-end so the first wiki regen is on disk before you walk away.
#
# Usage:
#   ./install.sh                           # interactive
#   ./install.sh --provider openai \
#                --api-key sk-... \
#                --model gpt-5-mini \
#                --workspace ~/.openclaw/workspace \
#                --cron "0 4 * * *"        # daily at 04:00 local
#   ./install.sh --no-cron                 # skip cron registration
#   ./install.sh --no-run                  # skip the first-run regen
#   ./install.sh -h                        # help

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
DEFAULT_WORKSPACE="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
DEFAULT_ENV_FILE="${OUROWIKI_ENV_FILE:-$HOME/.ourowiki/env}"
DEFAULT_CRON="0 4 * * *"   # daily 04:00 local

PROVIDER=""
API_KEY=""
MODEL=""
BASE_URL=""
WORKSPACE=""
ENV_FILE="$DEFAULT_ENV_FILE"
CRON_EXPR=""
SKIP_CRON=0
SKIP_RUN=0
NON_INTERACTIVE=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*" >&2; }
err()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()    { printf '\033[32m%s\033[0m\n' "$*"; }

usage() {
  cat <<EOF
ourowiki/install.sh — set up the wiki pipeline against an OpenClaw workspace.

Usage:
  ./install.sh [options]

Options:
  --provider <name>     anthropic | openai | openai-compat
  --api-key <key>       Provider API key (or any string for local servers)
  --model <id>          Override default model
  --base-url <url>      For openai / openai-compat (custom endpoint)
  --workspace <path>    OpenClaw workspace dir
                        (default: \$OPENCLAW_WORKSPACE or ~/.openclaw/workspace)
  --env-file <path>     Where to write the env config
                        (default: \$OUROWIKI_ENV_FILE or ~/.ourowiki/env)
  --cron "<expr>"       Cron schedule for the daily regen (default: "$DEFAULT_CRON")
  --no-cron             Do not register a cron entry
  --no-run              Skip the first-run pipeline pass
  --yes / --non-interactive
                        Don't prompt; require all needed values via flags
  -h / --help           This message

Provider env vars written to <env-file>:
  OUROWIKI_PROVIDER
  OUROWIKI_MODEL          (only if --model set)
  ANTHROPIC_API_KEY       (provider=anthropic)
  OPENAI_API_KEY          (provider=openai/openai-compat)
  OPENAI_BASE_URL         (only if --base-url set or non-default)

The env file is sourced by the cron job and by manual invocations of the
pipeline scripts.
EOF
}

# Read a value with a default, prompting if non-empty TTY available.
prompt_with_default() {
  local prompt_text="$1"
  local default="$2"
  local secret="${3:-}"   # any non-empty string -> secret prompt (no echo)
  local reply

  if [[ "$NON_INTERACTIVE" == 1 ]]; then
    printf '%s' "$default"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    # No TTY — fall back to default
    printf '%s' "$default"
    return 0
  fi

  if [[ -n "$secret" ]]; then
    printf '%s%s: ' "$prompt_text" \
      "$([ -n "$default" ] && printf ' [%s]' "${default:0:6}…")" >&2
    read -rs reply </dev/tty
    printf '\n' >&2
  else
    printf '%s%s: ' "$prompt_text" \
      "$([ -n "$default" ] && printf ' [%s]' "$default")" >&2
    read -r reply </dev/tty
  fi

  if [[ -z "$reply" ]]; then
    printf '%s' "$default"
  else
    printf '%s' "$reply"
  fi
}

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider)    PROVIDER="$2"; shift 2 ;;
    --api-key)     API_KEY="$2"; shift 2 ;;
    --model)       MODEL="$2"; shift 2 ;;
    --base-url)    BASE_URL="$2"; shift 2 ;;
    --workspace)   WORKSPACE="$2"; shift 2 ;;
    --env-file)    ENV_FILE="$2"; shift 2 ;;
    --cron)        CRON_EXPR="$2"; shift 2 ;;
    --no-cron)     SKIP_CRON=1; shift ;;
    --no-run)      SKIP_RUN=1; shift ;;
    --yes|--non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             err "unknown flag: $1"; usage >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve config (interactive or flag-driven)
# ---------------------------------------------------------------------------

bold "ourowiki install"
echo "  repo:      $REPO_DIR"

PROVIDER="${PROVIDER:-$(prompt_with_default 'Provider (anthropic / openai / openai-compat)' anthropic)}"
case "$PROVIDER" in
  anthropic|openai|openai-compat) ;;
  *) err "unknown provider: $PROVIDER (must be anthropic / openai / openai-compat)"; exit 2 ;;
esac

# Default model per provider.
if [[ -z "$MODEL" ]]; then
  case "$PROVIDER" in
    anthropic)   MODEL="$(prompt_with_default 'Model' claude-haiku-4-5)" ;;
    openai)      MODEL="$(prompt_with_default 'Model' gpt-5-mini)" ;;
    openai-compat) MODEL="$(prompt_with_default 'Model' qwen2.5-coder-32b)" ;;
  esac
fi

# Default base URL per provider.
if [[ -z "$BASE_URL" ]]; then
  case "$PROVIDER" in
    anthropic)     BASE_URL="" ;;  # use module default
    openai)        BASE_URL="$(prompt_with_default 'Base URL (blank = openai default)' '')" ;;
    openai-compat) BASE_URL="$(prompt_with_default 'Base URL' http://localhost:8080/v1)" ;;
  esac
fi

# API key (secret prompt). For openai-compat against an unauthenticated
# local server, any string works — we suggest "local-no-auth".
if [[ -z "$API_KEY" ]]; then
  case "$PROVIDER" in
    anthropic)
      API_KEY="$(prompt_with_default 'ANTHROPIC_API_KEY' '' secret)"
      ;;
    openai)
      API_KEY="$(prompt_with_default 'OPENAI_API_KEY' '' secret)"
      ;;
    openai-compat)
      API_KEY="$(prompt_with_default 'OPENAI_API_KEY (any non-empty string for unauth servers)' local-no-auth secret)"
      ;;
  esac
fi
if [[ -z "$API_KEY" ]]; then
  err "API key required for provider=$PROVIDER"
  exit 2
fi

WORKSPACE="${WORKSPACE:-$(prompt_with_default 'OpenClaw workspace' "$DEFAULT_WORKSPACE")}"
if [[ ! -d "$WORKSPACE" ]]; then
  warn "workspace dir does not exist yet: $WORKSPACE"
  if [[ "$NON_INTERACTIVE" == 1 ]]; then
    warn "  (continuing because --yes was given; mkdir on first run)"
    mkdir -p "$WORKSPACE/memory" "$WORKSPACE/scripts"
  else
    create="$(prompt_with_default 'Create it now? (yes/no)' yes)"
    if [[ "$create" == "yes" ]]; then
      mkdir -p "$WORKSPACE/memory" "$WORKSPACE/scripts"
      ok "  created $WORKSPACE"
    else
      err "aborting"; exit 2
    fi
  fi
fi

if [[ "$SKIP_CRON" == 0 && -z "$CRON_EXPR" ]]; then
  CRON_EXPR="$(prompt_with_default 'Cron schedule (blank to skip cron)' "$DEFAULT_CRON")"
  if [[ -z "$CRON_EXPR" ]]; then SKIP_CRON=1; fi
fi

# ---------------------------------------------------------------------------
# Write env file
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$ENV_FILE")"
{
  echo "# ourowiki provider config — generated by install.sh"
  echo "# This file is sourced by the cron job and by pipeline scripts."
  echo "# Keep it readable only by your user (chmod 600)."
  echo
  echo "export OUROWIKI_PROVIDER=$PROVIDER"
  if [[ -n "$MODEL" ]]; then
    echo "export OUROWIKI_MODEL=$MODEL"
  fi
  case "$PROVIDER" in
    anthropic)
      echo "export ANTHROPIC_API_KEY=$API_KEY"
      ;;
    openai|openai-compat)
      echo "export OPENAI_API_KEY=$API_KEY"
      if [[ -n "$BASE_URL" ]]; then
        echo "export OPENAI_BASE_URL=$BASE_URL"
      fi
      ;;
  esac
  echo "export OPENCLAW_WORKSPACE=$WORKSPACE"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "wrote $ENV_FILE (mode 600)"

# ---------------------------------------------------------------------------
# First-run pipeline pass
# ---------------------------------------------------------------------------

if [[ "$SKIP_RUN" == 0 ]]; then
  bold "First-run pipeline pass"
  if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not on PATH"; exit 2
  fi
  if ! python3 -c 'import httpx' >/dev/null 2>&1; then
    warn "httpx not installed; pip install --user httpx"
    if [[ "$NON_INTERACTIVE" == 1 ]]; then
      pip install --user httpx || { err "httpx install failed"; exit 2; }
    else
      ans="$(prompt_with_default 'Install httpx now? (yes/no)' yes)"
      if [[ "$ans" == "yes" ]]; then
        pip install --user httpx || { err "httpx install failed"; exit 2; }
      else
        err "aborting"; exit 2
      fi
    fi
  fi

  # shellcheck disable=SC1090
  source "$ENV_FILE"

  pushd "$WORKSPACE" >/dev/null

  set -x
  bash    "$REPO_DIR/scripts/wiki-extract.sh"
  python3 "$REPO_DIR/scripts/wiki-turns-extract.py"
  python3 "$REPO_DIR/scripts/wiki-turns-summarize.py"
  python3 "$REPO_DIR/scripts/wiki-sessions-compose.py"
  python3 "$REPO_DIR/scripts/wiki-month-pages.py"
  python3 "$REPO_DIR/scripts/wiki-compose.py"
  python3 "$REPO_DIR/scripts/wiki-entity-pages.py"
  python3 "$REPO_DIR/scripts/wiki-cross-refs.py"
  python3 "$REPO_DIR/scripts/wiki-implicit-links.py"
  python3 "$REPO_DIR/scripts/wiki-link-topics.py"
  set +x

  popd >/dev/null
  ok "first run complete"
else
  warn "skipping first run (--no-run)"
fi

# ---------------------------------------------------------------------------
# Cron registration
# ---------------------------------------------------------------------------

CRON_RUNNER="$REPO_DIR/scripts/ourowiki-cron.sh"

if [[ "$SKIP_CRON" == 0 ]]; then
  bold "Registering cron entry"

  # Build/refresh the runner script
  cat > "$CRON_RUNNER" <<'RUNNER_EOF'
#!/usr/bin/env bash
# ourowiki-cron.sh — invoked by cron to run the full pipeline once.
# Sources OUROWIKI_ENV_FILE, then runs each pipeline step in order.
# Output is logged to $OUROWIKI_LOG_FILE (default ~/.ourowiki/cron.log).

set -euo pipefail

ENV_FILE="${OUROWIKI_ENV_FILE:-$HOME/.ourowiki/env}"
LOG_FILE="${OUROWIKI_LOG_FILE:-$HOME/.ourowiki/cron.log}"

mkdir -p "$(dirname "$LOG_FILE")"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[ourowiki-cron] missing $ENV_FILE; run install.sh first" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)"

{
  echo "===== $(date -Iseconds) ourowiki-cron start ====="
  cd "$OPENCLAW_WORKSPACE"
  bash    "$REPO_DIR/scripts/wiki-extract.sh"
  python3 "$REPO_DIR/scripts/wiki-turns-extract.py"
  python3 "$REPO_DIR/scripts/wiki-turns-summarize.py"
  python3 "$REPO_DIR/scripts/wiki-sessions-compose.py"
  python3 "$REPO_DIR/scripts/wiki-month-pages.py"
  python3 "$REPO_DIR/scripts/wiki-compose.py"
  python3 "$REPO_DIR/scripts/wiki-entity-pages.py"
  python3 "$REPO_DIR/scripts/wiki-cross-refs.py"
  python3 "$REPO_DIR/scripts/wiki-implicit-links.py"
  python3 "$REPO_DIR/scripts/wiki-link-topics.py"
  echo "===== $(date -Iseconds) ourowiki-cron done  ====="
} >> "$LOG_FILE" 2>&1
RUNNER_EOF
  chmod +x "$CRON_RUNNER"
  ok "wrote $CRON_RUNNER"

  # Append to crontab if not already present.
  CRON_LINE="$CRON_EXPR OUROWIKI_ENV_FILE=$ENV_FILE $CRON_RUNNER"
  EXISTING="$(crontab -l 2>/dev/null || true)"
  if printf '%s' "$EXISTING" | grep -Fq "$CRON_RUNNER"; then
    warn "crontab already references $CRON_RUNNER; not duplicating"
  else
    {
      printf '%s\n' "$EXISTING"
      printf '%s\n' "# ourowiki — daily wiki regen"
      printf '%s\n' "$CRON_LINE"
    } | crontab -
    ok "appended to crontab: $CRON_LINE"
  fi
else
  warn "skipping cron registration (--no-cron)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

bold "Setup complete."
cat <<EOF

  Provider:    $PROVIDER ($MODEL)
  Workspace:   $WORKSPACE
  Env file:    $ENV_FILE
  Cron runner: $CRON_RUNNER

  Manual regen:    source $ENV_FILE && bash $CRON_RUNNER
  Inspect output:  ls $WORKSPACE/memory/wiki/
  Cron log:        ${OUROWIKI_LOG_FILE:-$HOME/.ourowiki/cron.log}

  See docs/wiki-architecture.md for what each pipeline step does.
EOF
