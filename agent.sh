#!/bin/sh
# Run a Claude Code agent on a Nevia computer. The agent clones a GitHub repo, deploys and
# tests its app, lets Claude work on the task (code only), tests the new code on a sandbox
# (checkpoint of the app + branch of its database) and opens a pull request for review (it
# never pushes to the base branch).
# See agent-bootstrap.sh for the steps that run on the agent computer.
#
# Usage: ./agent.sh setup [--rotate]                    create (or rotate) the agent's Nevia key, store the tokens
#        ./agent.sh run [--repo owner/name] ["task"]    run the agent on a task (asked for when not given), on the default repo or --repo
#        ./agent.sh logs <run>                          follow a run (it goes on if you stop following)
#        ./agent.sh ls                                  list the sandboxes left by previous runs
#        ./agent.sh preview <run>                       wake a run's sandbox and print its URL
#        ./agent.sh cleanup <run>                       delete a run's sandbox (computer, branch, secret, checkpoint)
#        ./agent.sh down                                sleep the agent computer
#        ./agent.sh destroy [--yes]                     delete the agent computer, its secrets and its Nevia principal
#        ./agent.sh help                                this help
#
# Config and tokens come from .env next to this script (see .env.example, ignored by git);
# GITHUB_TOKEN is copied into the workspace secret of the same name; Claude Code uses the login
# done once on the agent computer (nevia shell hello-agent, then claude and /login).
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)

# usage: the header comment of this file, up to the first line of code.
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
# A command is required: without one (or with -h/--help/help), print the help.
CMD=${1:-}
case $CMD in
  '')             usage; exit 1 ;;
  -h|--help|help) usage; exit 0 ;;
  run)
    # Without --repo, the default repo; without a task, it is asked for in run().
    shift
    REPO_ARG=
    case ${1:-} in
      --repo)   [ -n "${2:-}" ] || { printf 'error: --repo needs owner/name\n\n' >&2; usage >&2; exit 1; }
                REPO_ARG=$2; shift 2 ;;
      --repo=*) REPO_ARG=${1#--repo=}; shift ;;
    esac
    TASK=${1:-} ;;
  setup|ls|logs|preview|cleanup|down|destroy) ;;
  *)              printf 'error: unknown command "%s"\n\n' "$1" >&2; usage >&2; exit 1 ;;
esac

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# load_env <file>: export KEY=VALUE lines not already set in the shell (values never echoed).
load_env() {
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    key=${line%%=*}; val=${line#*=}
    case $key in ''|*[!A-Za-z0-9_]*) die "$1: invalid variable name '$key'" ;; esac
    case $val in \"*\") val=${val#\"}; val=${val%\"} ;; \'*\') val=${val#\'}; val=${val%\'} ;; esac
    eval "[ -n \"\${$key+x}\" ]" || export "$key=$val"
  done < "$1"
}
load_env "$DIR/.env"

AGENT=${AGENT:-hello-agent}
BASE_BRANCH=${BASE_BRANCH:-main}
# The application the agent works on: its folder in the repo, and its stack on Nevia.
APP_DIR=${APP_DIR:-.}
BASE_COMPUTER=${APP_COMPUTER:-hello-app}
BASE_DATABASE=${APP_DATABASE:-hello-pg}
CLAUDE_MODEL=${CLAUDE_MODEL:-}
KEY_TTL=${KEY_TTL:-30d}
KEY_SECRET=NEVIA_AGENT_KEY
# github_repo <owner/name or GitHub URL>: owner/name.
github_repo() { printf '%s' "$1" | sed -E 's#^(git@github\.com:|https?://github\.com/)##; s#/+$##; s#\.git$##'; }
# Default repo to clone: REPO, else the origin of the git repo you run from, else the hello world app.
DEFAULT_REPO=bertrandcedric/nevia-helloworld
REPO=$(github_repo "${REPO:-$(git remote get-url origin 2>/dev/null || echo "$DEFAULT_REPO")}")

command -v nevia >/dev/null || die "nevia CLI not found: curl -fsSL https://get.nevia.cloud | sh"
command -v jq >/dev/null || die "jq not found: brew install jq"
[ -n "${NEVIA_WORKSPACE:-}" ] || die "set NEVIA_WORKSPACE in .env: the agent needs it too"

nv() { nevia --non-interactive "$@"; }

# status_json <computer|pg> <name>: the resource as JSON, nothing if it doesn't exist (exit 3).
status_json() {
  if [ "$1" = pg ]; then set -- pg status "$2"; else set -- status "$2"; fi
  rc=0; out=$(nv --json "$@") || rc=$?
  case $rc in
    0) printf '%s' "$out" ;;
    3) ;;
    *) die "nevia $* failed (exit $rc)" ;;
  esac
}
json_field() { printf '%s' "$1" | jq -r --arg k "$2" 'first(.. | objects | select(has($k)) | .[$k]) // empty'; }
has_secret() { nv --json secrets ls | jq -e --arg n "$1" 'any(.secrets[]; .name == $n)' >/dev/null; }

# The token from .env (or the shell) goes straight into a write-only workspace secret, via stdin.
store_tokens() {
  # Catch a value pasted into the wrong line before it reaches the agent (format only, never printed).
  case ${GITHUB_TOKEN:-} in ''|github_pat_*|ghp_*) ;; *) die "GITHUB_TOKEN in .env doesn't look like a GitHub token (github_pat_…)" ;; esac
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    printf '%s' "$GITHUB_TOKEN" | nv --yes secrets GITHUB_TOKEN --file - >/dev/null
    log "updated workspace secret GITHUB_TOKEN"
  else
    has_secret GITHUB_TOKEN || die "missing GITHUB_TOKEN: set it in .env (see .env.example)"
  fi
}

# The agent's own Nevia identity: a key-file principal (editor) whose key file goes straight
# into the workspace secret NEVIA_AGENT_KEY, never to disk.
setup() {
  store_tokens
  if has_secret "$KEY_SECRET" && [ "${1:-}" != --rotate ]; then
    log "workspace secret $KEY_SECRET already set (./agent.sh setup --rotate for a new key)"
    return
  fi
  principal=$(nv --json access ls | jq -r --arg n "$AGENT" 'first(.access[] | select(.principal.name == $n) | .principal.id) // empty')
  if [ -z "$principal" ]; then
    log "creating Nevia principal $AGENT (role editor, key valid $KEY_TTL)"
    key=$(nv --yes access add --private-key --role editor --name "$AGENT" \
            --description "Claude Code agent (agent.sh)" --expires-in "$KEY_TTL" --out -)
  else
    log "rotating the key of Nevia principal $AGENT ($principal, valid $KEY_TTL)"
    key=$(nv --yes access keys rotate "$principal" --expires-in "$KEY_TTL" --out -)
  fi
  [ -n "$key" ] || die "no key file returned for $AGENT"
  printf '%s\n' "$key" | nv --yes secrets "$KEY_SECRET" --file - >/dev/null
  unset key
  log "stored the agent's key in workspace secret $KEY_SECRET"
}

agent_up() {
  store_tokens
  has_secret "$KEY_SECRET" || die "the agent has no Nevia key yet: ./agent.sh setup"

  set -- -e "GITHUB_TOKEN={{secrets.GITHUB_TOKEN}}" \
         -e "AGENT_NEVIA_KEY={{secrets.$KEY_SECRET}}" -e "AGENT_NEVIA_WORKSPACE=$NEVIA_WORKSPACE" \
         -e "REPO=$REPO" -e "BASE_BRANCH=$BASE_BRANCH" \
         -e "APP_DIR=$APP_DIR" -e "BASE_COMPUTER=$BASE_COMPUTER" -e "BASE_DATABASE=$BASE_DATABASE"
  [ -z "$CLAUDE_MODEL" ] || set -- "$@" -e "CLAUDE_MODEL=$CLAUDE_MODEL"

  json=$(status_json computer "$AGENT")
  if [ -z "$json" ]; then
    log "creating agent computer $AGENT (no public port)"
    nv new "$AGENT" "$@" --wait >/dev/null
    die "new $AGENT: log Claude Code in once (nevia shell $AGENT, then claude and /login), then: ./agent.sh run \"<task>\""
  else
    state=$(json_field "$json" state)
    log "agent computer $AGENT is $state"
    if [ "$state" = sleeping ]; then nv awake "$AGENT" --wait >/dev/null; fi
    # Don't leave it awake if its settings are refused.
    nv set "$AGENT" "$@" >/dev/null || { nv suspend "$AGENT" >/dev/null 2>&1 || true; die "could not configure $AGENT"; }
  fi
}

# pick_repo: the repo to clone, from --repo, else the default $REPO.
pick_repo() {
  [ -z "$REPO_ARG" ] || REPO=$REPO_ARG
  REPO=$(github_repo "$REPO")
  case $REPO in
    */*/*|/*|*/|*[!A-Za-z0-9_./-]*) ;;
    */*) return ;;
  esac
  die "not a GitHub repo: '$REPO' (expected owner/name)"
}

# The task is required: the agent evaluates and implements exactly what it is asked.
# Given as an argument, or asked for in a terminal; without a terminal (CI, pipe), an error.
pick_task() {
  case $TASK in *[![:space:]]*) return ;; esac
  [ -t 0 ] || { printf 'error: run needs a task, e.g. ./agent.sh run "add a GET /visits endpoint returning the 10 latest visits"\n\n' >&2
                usage >&2; exit 1; }
  printf 'Describe the task for the agent:\n'
  while :; do
    printf '> '
    IFS= read -r TASK || exit 1
    case $TASK in *[![:space:]]*) break ;; esac
  done
}

# A run lasts longer than one `nevia exec` session holds, so it runs detached on the agent
# (~/agent/runs/<run>.sh, .log, .status) and this side follows the log with short execs.
run() {
  pick_repo
  pick_task
  agent_up
  RUN=$(date -u +%y%m%d%H%M%S)
  log "run $RUN: agent on $REPO ($BASE_BRANCH), base stack $BASE_COMPUTER + $BASE_DATABASE"
  nevia exec "$AGENT" -- sh -c 'mkdir -p ~/agent/runs && cat > ~/agent/runs/"$1".sh' sh "$RUN" < "$DIR/agent-bootstrap.sh"
  # base64 keeps any quoting in the task intact through nevia exec
  nevia exec -n "$AGENT" -- sh -c '
    cd ~/agent/runs &&
    setsid sh -c "sh ./\$1.sh \"\$1\" \"\$2\" > \$1.log 2>&1; echo \$? > \$1.status" sh "$1" "$2" \
      < /dev/null > /dev/null 2>&1 &' sh "$RUN" "$(printf '%s' "$TASK" | base64 | tr -d '\n')"
  log "started on $AGENT (Ctrl-C stops following, not the run: ./agent.sh logs $RUN)"
  finish "$RUN"
}

# follow <run>: print the run's new log lines until it writes its exit status; returns it.
follow() {
  n=0 fails=0
  while :; do
    # Prints the complete lines after line $2, then "@@ <line count> <status, once done>".
    if out=$(nevia exec -n "$AGENT" -- sh -c '
          f=~/agent/runs/$1.log
          [ -f "$f" ] || { echo "@@ 0 missing"; exit 0; }
          n=$(wc -l < "$f")
          [ "$n" -le "$2" ] || sed -n "$(($2 + 1)),${n}p" "$f"
          echo "@@ $n $(cat ~/agent/runs/$1.status 2>/dev/null)"' sh "$1" "$n" 2>/dev/null); then
      fails=0
      printf '%s\n' "$out" | sed '$d'
      set -- "$1" $(printf '%s\n' "$out" | tail -n 1)
      n=$3
      case ${4:-} in
        '') ;;
        missing) die "no run $1 on $AGENT" ;;
        *) return "$4" ;;
      esac
    else
      fails=$((fails + 1))
      [ $fails -lt 12 ] || die "lost contact with $AGENT; the run may still go on: ./agent.sh logs $1"
    fi
    sleep 5
  done
}

# finish <run>: follow the run, then put the agent to sleep.
finish() {
  rc=0; follow "$1" || rc=$?
  log "putting $AGENT to sleep"
  nv suspend "$AGENT" --wait >/dev/null || true
  [ $rc -eq 0 ] || die "run $1 failed (exit $rc); its sandbox, if any: ./agent.sh cleanup $1"
}

logs() {
  sandbox_names "${1:-}"
  json=$(status_json computer "$AGENT")
  [ -n "$json" ] || die "no agent computer $AGENT"
  if [ "$(json_field "$json" state)" = sleeping ]; then nv awake "$AGENT" --wait >/dev/null; fi
  finish "$1"
}

# A run's sandbox names, as agent-bootstrap.sh builds them.
sandbox_names() {
  [ -n "${1:-}" ] || die "which run? ./agent.sh ls"
  case $1 in *[!0-9]*) die "a run id is digits only, got '$1'" ;; esac
  SANDBOX_COMPUTER=$BASE_COMPUTER-$1 SANDBOX_DATABASE=$BASE_DATABASE-$1
  SANDBOX_SECRET=AGENT_DATABASE_URL_$1 CHECKPOINT=agent-$1
}

ls_sandboxes() {
  nv --json ls | jq -r --arg p "$BASE_COMPUTER-" '
    .computers[] | select(.name | startswith($p)) | select(.name | ltrimstr($p) | test("^[0-9]+$"))
    | "\(.name | ltrimstr($p))\t\(.name)\t\(.state)\t\(first(.ports[]?.url) // "-")"' |
    { printf 'RUN\tCOMPUTER\tSTATE\tURL\n'; cat; } | column -t -s "$(printf '\t')"
}

preview() {
  sandbox_names "${1:-}"
  json=$(status_json computer "$SANDBOX_COMPUTER")
  [ -n "$json" ] || die "no sandbox for run $1"
  log "waking $SANDBOX_DATABASE and $SANDBOX_COMPUTER"
  nv pg awake "$SANDBOX_DATABASE" --wait >/dev/null 2>&1 || true
  nv awake "$SANDBOX_COMPUTER" --wait >/dev/null 2>&1 || true
  log "preview: $(json_field "$json" url)"
}

# Idempotent; the computer goes first: a checkpoint can't be deleted while a computer restored
# from it is live.
cleanup() {
  sandbox_names "${1:-}"
  if [ -n "$(status_json computer "$SANDBOX_COMPUTER")" ]; then
    log "deleting computer $SANDBOX_COMPUTER"; nv --yes rm "$SANDBOX_COMPUTER" --wait >/dev/null
  fi
  if [ -n "$(status_json pg "$SANDBOX_DATABASE")" ]; then
    log "deleting database branch $SANDBOX_DATABASE"; nv --yes pg rm "$SANDBOX_DATABASE" --wait >/dev/null
  fi
  # HELLO_DATABASE_URL_<run>: name used by runs made before the agent and the app were split
  for secret in "$SANDBOX_SECRET" "HELLO_DATABASE_URL_$1"; do
    if has_secret "$secret"; then
      log "deleting secret $secret"; nv --yes secrets unset "$secret" >/dev/null
    fi
  done
  if nv checkpoint status -c "$BASE_COMPUTER" "$CHECKPOINT" >/dev/null 2>&1; then
    log "deleting checkpoint $CHECKPOINT"; nv --yes checkpoint rm -c "$BASE_COMPUTER" "$CHECKPOINT" >/dev/null
  fi
  log "run $1 cleaned up"
}

# Deletes the agent: its computer, its workspace secrets and its Nevia principal (and so its key).
# Idempotent: whatever is already gone is skipped. The sandboxes stay (PR previews): ./agent.sh cleanup.
destroy() {
  if [ "${1:-}" != --yes ]; then
    [ -t 0 ] || die "destroy needs confirmation: $0 destroy --yes"
    printf 'Delete agent computer %s, secrets GITHUB_TOKEN, %s and principal %s? Type "destroy": ' \
      "$AGENT" "$KEY_SECRET" "$AGENT"
    read -r answer
    [ "$answer" = destroy ] || die "aborted"
  fi
  if [ -n "$(status_json computer "$AGENT")" ]; then
    log "deleting agent computer $AGENT"
    nv --yes rm "$AGENT" --wait >/dev/null
  else
    log "agent computer $AGENT already gone"
  fi
  for secret in GITHUB_TOKEN "$KEY_SECRET"; do
    if has_secret "$secret"; then
      log "deleting secret $secret"; nv --yes secrets unset "$secret" >/dev/null
    else
      log "secret $secret already gone"
    fi
  done
  principal=$(nv --json access ls | jq -r --arg n "$AGENT" 'first(.access[] | select(.principal.name == $n) | .principal.id) // empty')
  if [ -n "$principal" ]; then
    log "deleting Nevia principal $AGENT ($principal) and its key"; nv --yes access rm "$principal" >/dev/null
  else
    log "Nevia principal $AGENT already gone"
  fi
  log "kept: the sandboxes (./agent.sh ls, then ./agent.sh cleanup <run>); start again with ./agent.sh setup"
}

case $CMD in
  run)     run ;;
  setup)   setup "${2:-}" ;;
  ls)      ls_sandboxes ;;
  logs)    logs "${2:-}" ;;
  preview) preview "${2:-}" ;;
  cleanup) cleanup "${2:-}" ;;
  down)    nv suspend "$AGENT" --wait >/dev/null; log "slept $AGENT" ;;
  destroy) destroy "${2:-}" ;;
esac
