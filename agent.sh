#!/bin/sh
# Run a Claude Code agent on a Nevia computer. The agent clones a GitHub repo, deploys and
# tests its app, lets Claude work on the task (code only), tests the new code on a sandbox
# (checkpoint of the app + branch of its database) and opens a pull request for review (it
# never pushes to the base branch).
#
# Usage: ./agent.sh setup [--rotate]                    create the agent (computer, Nevia key, GitHub token), once
#        ./agent.sh run [--repo owner/name] ["task"]    run the agent on a task (asked for when not given), on REPO or --repo
#        ./agent.sh logs [<run>]                        follow a run, by default the latest (it goes on if you stop following)
#        ./agent.sh ls                                  list the sandboxes left by previous runs
#        ./agent.sh preview <run>                       wake a run's sandbox and print its URL
#        ./agent.sh cleanup <run>                       delete a run's sandbox (computer, branch, secret, checkpoint)
#        ./agent.sh down                                put the agent computer to sleep (unless a run is going)
#        ./agent.sh destroy [--yes]                     delete the agent computer, its secrets and its Nevia principal
#        ./agent.sh help                                this help
#
# Only setup and destroy manage Nevia from this machine. The other commands wake the agent
# computer if it sleeps, push agent-bootstrap.sh to it and follow it: git, the build, Claude and
# the Nevia resources are all handled there, and the agent goes back to sleep once idle.
# Config comes from .env next to this script (see .env.example, ignored by git); Claude Code
# uses the login done once on the agent computer (nevia shell hello-agent, then claude and /login).
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)

# usage: the header comment of this file, up to the first line of code.
usage() { sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0"; }
# A command is required: without one (or with -h/--help/help), print the help.
CMD=${1:-}
case $CMD in
  '')             usage; exit 1 ;;
  -h|--help|help) usage; exit 0 ;;
  run)
    # Without --repo, REPO from .env; without a task, it is asked for in run().
    shift
    REPO_ARG=
    if [ "${1:-}" = --repo ]; then
      [ -n "${2:-}" ] || { printf 'error: --repo needs owner/name\n\n' >&2; usage >&2; exit 1; }
      REPO_ARG=$2; shift 2
    fi
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
# The application the agent works on: its stack on Nevia.
BASE_COMPUTER=${APP_COMPUTER:-hello-app}
BASE_DATABASE=${APP_DATABASE:-hello-pg}
CLAUDE_MODEL=${CLAUDE_MODEL:-}
KEY_TTL=${KEY_TTL:-30d}
KEY_SECRET=NEVIA_AGENT_KEY
REPO=${REPO:-}

command -v nevia >/dev/null || die "nevia CLI not found: curl -fsSL https://get.nevia.cloud | sh"
[ -n "${NEVIA_WORKSPACE:-}" ] || die "set NEVIA_WORKSPACE in .env"

nv() { nevia --non-interactive "$@"; }

# --- on the agent: wake it, push agent-bootstrap.sh, follow -------------------------------

# on_agent <nevia exec args>...: nevia exec on the agent, with the config from .env (the secrets
# are in the computer's own env, see setup).
on_agent() {
  nv exec -e "AGENT=$AGENT" -e "AGENT_NEVIA_WORKSPACE=$NEVIA_WORKSPACE" \
    -e "REPO=$REPO" -e "BASE_BRANCH=$BASE_BRANCH" -e "BASE_COMPUTER=$BASE_COMPUTER" \
    -e "BASE_DATABASE=$BASE_DATABASE" ${CLAUDE_MODEL:+-e "CLAUDE_MODEL=$CLAUDE_MODEL"} "$@"
}

# wake: nevia exec never wakes a computer (it exits 5), so wake the agent first when it sleeps.
# Exit 6 (busy or unreachable, e.g. while it changes state) is retried.
wake() {
  i=0
  while :; do
    rc=0; nv exec -n "$AGENT" -- true >/dev/null 2>&1 || rc=$?
    case $rc in
      0) return ;;
      5) log "waking $AGENT"; nv awake "$AGENT" --wait >/dev/null; return ;;
      3) die "no agent computer $AGENT: ./agent.sh setup" ;;
      6) i=$((i + 1)); [ $i -lt 12 ] || die "cannot reach $AGENT (nevia exec exit 6, busy or unreachable)"; sleep 5 ;;
      *) die "cannot reach $AGENT (nevia exec exit $rc)" ;;
    esac
  done
}

# remote <command> [<run>]: agent-bootstrap.sh <command> on the agent, which then sleeps if idle.
remote() {
  wake
  on_agent "$AGENT" -- sh -s "$@" < "$DIR/agent-bootstrap.sh"
}

run_id() {
  [ -n "${1:-}" ] || die "which run? ./agent.sh ls"
  case $1 in *[!0-9]*) die "a run id is digits only, got '$1'" ;; esac
}

# pick_repo: the repo to clone (owner/name), from --repo, else REPO from .env.
pick_repo() {
  [ -z "$REPO_ARG" ] || REPO=$REPO_ARG
  [ -n "$REPO" ] || die "no GitHub repo: set REPO=owner/name in .env, or use --repo owner/name"
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

# A run lasts longer than one `nevia exec` session holds, so a single exec stores the script on
# the agent (~/agent/runs/<run>.sh) and starts it detached (.log, then .status); once done, the
# agent goes to sleep on its own, after leaving time to read the end of the log.
run() {
  pick_repo
  pick_task
  wake
  RUN=$(date -u +%y%m%d%H%M%S)
  log "run $RUN: agent on $REPO ($BASE_BRANCH), base stack $BASE_COMPUTER + $BASE_DATABASE"
  # base64 keeps any quoting in the task intact through nevia exec
  # The script is read before going to the background: once this exec returns, stdin is gone.
  on_agent "$AGENT" -- sh -c '
    mkdir -p ~/agent/runs && cd ~/agent/runs && cat > "$1.sh" || exit
    [ -s "$1.sh" ] || { echo "error: agent-bootstrap.sh arrived empty" >&2; exit 1; }
    setsid sh -c "sh ./\$1.sh run \"\$1\" \"\$2\" > \$1.log 2>&1; echo \$? > \$1.status; sh ./\$1.sh sleep 20 > /dev/null 2>&1" \
      sh "$1" "$2" < /dev/null > /dev/null 2>&1 &' sh "$RUN" "$(printf '%s' "$TASK" | base64 | tr -d '\n')" \
    < "$DIR/agent-bootstrap.sh"
  log "started on $AGENT (Ctrl-C stops following, not the run: ./agent.sh logs $RUN)"
  rc=0; follow "$RUN" || rc=$?
  [ $rc -eq 0 ] || die "run $RUN failed (exit $rc); its sandbox, if any: ./agent.sh cleanup $RUN"
}

# follow <run>: print the run's new log lines until it writes its exit status; returns it.
follow() {
  n=0 fails=0
  while :; do
    # Prints the complete lines after line $2, then "@@ <line count> <status, once done>".
    rc=0
    out=$(nv exec -n "$AGENT" -- sh -c '
          f=~/agent/runs/$1.log
          [ -f "$f" ] || { echo "@@ 0 missing"; exit 0; }
          n=$(wc -l < "$f")
          [ "$n" -le "$2" ] || sed -n "$(($2 + 1)),${n}p" "$f"
          echo "@@ $n $(cat ~/agent/runs/$1.status 2>/dev/null)"' sh "$1" "$n" 2>/dev/null) || rc=$?
    case $rc in
      0)
        fails=0
        printf '%s\n' "$out" | sed '$d'
        set -- "$1" $(printf '%s\n' "$out" | tail -n 1)
        n=$3
        case ${4:-} in
          '') ;;
          missing) die "no run $1 on $AGENT" ;;
          *) return "$4" ;;
        esac ;;
      5) die "$AGENT went to sleep: run $1 is over (./agent.sh logs $1 for its log)" ;;
      *)
        fails=$((fails + 1))
        [ $fails -lt 12 ] || die "lost contact with $AGENT; the run may still go on: ./agent.sh logs $1" ;;
    esac
    sleep 5
  done
}

# logs [<run>]: attach to a run, by default the latest one.
logs() {
  wake
  set -- "${1:-$(nv exec -n "$AGENT" -- sh -c 'ls -t ~/agent/runs/*.log 2>/dev/null | head -n 1 | xargs -r basename -s .log')}"
  [ -n "$1" ] || die "no run on $AGENT yet: ./agent.sh run"
  run_id "$1"
  log "run $1"
  rc=0; follow "$1" || rc=$?
  # Woken to read a finished run: back to sleep (a run still going sleeps on its own).
  remote sleep
  [ $rc -eq 0 ] || die "run $1 failed (exit $rc); its sandbox, if any: ./agent.sh cleanup $1"
}

down() {
  rc=0; nv exec -n "$AGENT" -- true >/dev/null 2>&1 || rc=$?
  if [ $rc -eq 5 ]; then log "$AGENT already sleeps"; else remote sleep; fi
}

# --- setup and destroy: the only Nevia management done from this machine ------------------

has_secret() { nv --json secrets ls | jq -e --arg n "$1" 'any(.secrets[]; .name == $n)' >/dev/null; }
principal_id() { nv --json access ls | jq -r --arg n "$AGENT" 'first(.access[] | select(.principal.name == $n) | .principal.id) // empty'; }

# status_json <name>: the computer as JSON, nothing if it doesn't exist (exit 3).
status_json() {
  rc=0; out=$(nv --json status "$1") || rc=$?
  case $rc in
    0) printf '%s' "$out" ;;
    3) ;;
    *) die "nevia status $1 failed (exit $rc)" ;;
  esac
}

# The agent's GitHub token and Nevia identity (an editor key-file principal) go straight into
# write-only workspace secrets, via stdin; the agent computer's env references them.
setup() {
  command -v jq >/dev/null || die "jq not found: brew install jq"
  # Catch a value pasted into the wrong line before it reaches the agent (format only, never printed).
  case ${GITHUB_TOKEN:-} in ''|github_pat_*|ghp_*) ;; *) die "GITHUB_TOKEN in .env doesn't look like a GitHub token (github_pat_…)" ;; esac
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    printf '%s' "$GITHUB_TOKEN" | nv --yes secrets GITHUB_TOKEN --file - >/dev/null
    log "updated workspace secret GITHUB_TOKEN"
  else
    has_secret GITHUB_TOKEN || die "missing GITHUB_TOKEN: set it in .env (see .env.example)"
  fi

  if has_secret "$KEY_SECRET" && [ "${1:-}" != --rotate ]; then
    log "workspace secret $KEY_SECRET already set (./agent.sh setup --rotate for a new key)"
  else
    principal=$(principal_id)
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
  fi

  set -- -e "GITHUB_TOKEN={{secrets.GITHUB_TOKEN}}" -e "AGENT_NEVIA_KEY={{secrets.$KEY_SECRET}}"
  json=$(status_json "$AGENT")
  if [ -z "$json" ]; then
    log "creating agent computer $AGENT (no public port)"
    nv new "$AGENT" "$@" --wait >/dev/null
    log "now log Claude Code in once: nevia shell $AGENT, then claude and /login; then ./agent.sh run"
  else
    log "agent computer $AGENT exists: updating its env"
    nv set "$AGENT" "$@" >/dev/null
  fi
}

# Deletes the agent: its computer, its workspace secrets and its Nevia principal (and so its key).
# Idempotent: whatever is already gone is skipped. The sandboxes stay (PR previews): ./agent.sh cleanup.
destroy() {
  command -v jq >/dev/null || die "jq not found: brew install jq"
  if [ "${1:-}" != --yes ]; then
    [ -t 0 ] || die "destroy needs confirmation: $0 destroy --yes"
    printf 'Delete agent computer %s, secrets GITHUB_TOKEN, %s and principal %s? Type "destroy": ' \
      "$AGENT" "$KEY_SECRET" "$AGENT"
    read -r answer
    [ "$answer" = destroy ] || die "aborted"
  fi
  if [ -n "$(status_json "$AGENT")" ]; then
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
  principal=$(principal_id)
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
  ls)      remote ls ;;
  logs)    logs "${2:-}" ;;
  preview) run_id "${2:-}"; remote preview "$2" ;;
  cleanup) run_id "${2:-}"; remote cleanup "$2" ;;
  down)    down ;;
  destroy) destroy "${2:-}" ;;
esac
