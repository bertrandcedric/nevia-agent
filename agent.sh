#!/bin/sh
# Migrate an app from MongoDB to Postgres with a Claude Code agent on Nevia: it opens a PR.
# Config: .env only (see .env.example).
#
# Usage: ./agent.sh setup [--rotate]     create the agent, once
#        ./agent.sh migrate              run the migration of REPO
#        ./agent.sh logs [<run>]         follow a run (default: the latest)
#        ./agent.sh stop [<run>]         stop a run (default: the one going)
#        ./agent.sh ls                   list the sandboxes
#        ./agent.sh preview <run>        wake a sandbox, print its URL
#        ./agent.sh cleanup <run>        delete a sandbox
#        ./agent.sh purge [--yes]        delete every run, the app and its databases
#        ./agent.sh down                 put the agent to sleep
#        ./agent.sh destroy [--yes]      delete the agent
#        ./agent.sh help                 this help
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)

# usage: the header comment of this file, up to the first line of code.
usage() { sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0"; }
# A command is required: without one (or with -h/--help/help), print the help.
CMD=${1:-}
case $CMD in
  '')             usage; exit 1 ;;
  -h|--help|help) usage; exit 0 ;;
  migrate)
    # The repo is REPO in .env, the task the file's: migrate takes no argument.
    if [ -n "${2:-}" ]; then
      printf 'error: migrate takes no argument: set REPO in .env, edit tasks/mongo-to-postgres.txt for the task\n\n' >&2
      usage >&2; exit 1
    fi ;;
  setup|ls|logs|stop|preview|cleanup|purge|down|destroy) ;;
  *)              printf 'error: unknown command "%s"\n\n' "$1" >&2; usage >&2; exit 1 ;;
esac

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The settings read from .env: whatever the shell has for them is dropped, so .env alone decides.
unset NEVIA_WORKSPACE GITHUB_TOKEN REPO BASE_BRANCH CLAUDE_MODEL CLAUDE_MAX_TURNS AGENT KEY_TTL \
  APP_COMPUTER MIGRATION_DIR
# load_env <file>: export its KEY=VALUE lines (values never echoed).
load_env() {
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    key=${line%%=*}; val=${line#*=}
    case $key in ''|*[!A-Za-z0-9_]*) die "$1: invalid variable name '$key'" ;; esac
    case $val in \"*\") val=${val#\"}; val=${val%\"} ;; \'*\') val=${val#\'}; val=${val%\'} ;; esac
    export "$key=$val"
  done < "$1"
}
load_env "$DIR/.env"

AGENT=${AGENT:-hello-agent}
BASE_BRANCH=${BASE_BRANCH:-main}
# The application the agent works on: its stack on Nevia.
BASE_COMPUTER=${APP_COMPUTER:-hello-app}
CLAUDE_MODEL=${CLAUDE_MODEL:-}
CLAUDE_MAX_TURNS=${CLAUDE_MAX_TURNS:-}
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
    ${CLAUDE_MODEL:+-e "CLAUDE_MODEL=$CLAUDE_MODEL"} \
    ${CLAUDE_MAX_TURNS:+-e "CLAUDE_MAX_TURNS=$CLAUDE_MAX_TURNS"} "$@"
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

# pick_repo: the repo to clone (owner/name), REPO from .env.
pick_repo() {
  [ -n "$REPO" ] || die "no GitHub repo: set REPO=owner/name in .env"
  case $REPO in
    */*/*|/*|*/|*[!A-Za-z0-9_./-]*) ;;
    */*) return ;;
  esac
  die "not a GitHub repo: '$REPO' (expected owner/name)"
}

# The task of `migrate`, in its own file to be read and edited as text. Claude sees only the clone
# (no Nevia, no git, no deploy layer), so the file spells out what the application receives.
MIGRATION_TASK_FILE=$DIR/tasks/mongo-to-postgres.txt
# The mongo2pg project of a migration, made beforehand by `mongo2pg infer -c` (see README.md): by
# default migration/<repo name>/ here, one per repository, ignored by git.
MIGRATION_DIR=${MIGRATION_DIR:-}

# push_deploy: send deploy/ to the agent computer, where agent-bootstrap.sh runs it as
# ~/agent/deploy/deploy.sh. Pushed at every run, so a change here takes effect on the next one.
push_deploy() {
  log "pushing the deploy layer to $AGENT"
  # macOS bsdtar would add xattrs/AppleDouble files; GNU tar has no such flags.
  if tar --version 2>/dev/null | grep -q bsdtar; then tflags="--no-xattrs --no-mac-metadata"; else tflags=; fi
  # shellcheck disable=SC2086
  COPYFILE_DISABLE=1 tar $tflags -czf - -C "$DIR" deploy | base64 |
    nv exec "$AGENT" -- sh -c 'rm -rf ~/agent/deploy && mkdir -p ~/agent &&
      base64 -d | tar -xzf - -C ~/agent && chmod +x ~/agent/deploy/deploy.sh' ||
    die "could not push the deploy layer to $AGENT"
}

# push_analysis <run>: send the mongo2pg analysis in MIGRATION_DIR to the agent, as
# ~/agent/runs/<run>.analysis, read-only. Only what Claude reads: the inferred schemas, the stats
# and the mappings (source/collections/), the generated SQL (schema/tables/). The sample values
# they quote are real data, so they are removed here, before anything leaves this machine; config/
# (URIs), data/ and the HTML reports stay here too.
push_analysis() {
  command -v jq >/dev/null || die "jq not found: it removes the sample values from the analysis"
  tmp=$(mktemp -d)
  mkdir "$tmp/analysis"
  cp -R "$MIGRATION_DIR/source/collections" "$tmp/analysis/collections"
  [ ! -d "$MIGRATION_DIR/schema/tables" ] || cp -R "$MIGRATION_DIR/schema/tables" "$tmp/analysis/tables"
  for f in $(find "$tmp/analysis" -name '*.json'); do
    jq 'del(.. | .values?)' "$f" > "$f.new" && mv "$f.new" "$f" || { rm -rf "$tmp"; die "cannot read $f"; }
  done
  # The examples of an infer warning: the lines under `examples:` more indented than it, or list items.
  for f in $(find "$tmp/analysis" -name '*.stats.yaml'); do
    awk '/^ *examples:/ { match($0, /^ */); ind = RLENGTH; skip = 1; next }
         skip { match($0, /^ */); if (RLENGTH > ind || substr($0, RLENGTH + 1, 2) == "- ") next; skip = 0 }
         { print }' "$f" > "$f.new" && mv "$f.new" "$f"
  done
  log "pushing the mongo2pg analysis of $MIGRATION_DIR to $AGENT (without its sample values)"
  if tar --version 2>/dev/null | grep -q bsdtar; then tflags="--no-xattrs --no-mac-metadata"; else tflags=; fi
  # shellcheck disable=SC2086
  COPYFILE_DISABLE=1 tar $tflags -czf - -C "$tmp" analysis | base64 |
    nv exec "$AGENT" -- sh -c 'd=~/agent/runs/$1.analysis && mkdir -p "$d" &&
      base64 -d | tar -xzf - -C "$d" --strip-components=1 && chmod -R a-w "$d"' sh "$1" ||
    { rm -rf "$tmp"; die "could not push the analysis to $AGENT"; }
  rm -rf "$tmp"
}

# A run lasts longer than one `nevia exec` session holds, so a single exec stores the script on
# the agent (~/agent/runs/<run>.sh) and starts it detached (.log, then .status); once done, the
# agent goes to sleep on its own, after leaving time to read the end of the log.
migrate() {
  [ -s "$MIGRATION_TASK_FILE" ] || die "no task in $MIGRATION_TASK_FILE"
  pick_repo
  MIGRATION_DIR=${MIGRATION_DIR:-$DIR/migration/${REPO##*/}}
  [ -d "$MIGRATION_DIR/source/collections" ] ||
    die "no mongo2pg analysis in $MIGRATION_DIR: run mongo2pg infer -c <config> first (see README.md), or set MIGRATION_DIR"
  TASK=$(cat "$MIGRATION_TASK_FILE")
  log "task: the MongoDB -> Postgres migration ($MIGRATION_TASK_FILE), analysis: $MIGRATION_DIR"
  wake
  # One run at a time on the agent: pushing deploy/ and the analysis would change them under it.
  going=$(nv exec "$AGENT" -- sh -c 'for f in ~/agent/runs/*.pid; do
      [ -e "$f" ] && [ ! -e "${f%.pid}.status" ] && kill -0 "$(cat "$f")" 2>/dev/null && basename "$f" .pid
    done; true')
  [ -z "$going" ] || die "run $going is still going on $AGENT: ./agent.sh logs $going, then start again"
  push_deploy
  RUN=$(date -u +%y%m%d%H%M%S)
  log "run $RUN: agent on $REPO ($BASE_BRANCH), base app $BASE_COMPUTER"
  push_analysis "$RUN"
  # base64 keeps any quoting in the task intact through nevia exec
  # The script is read before going to the background: once this exec returns, stdin is gone.
  on_agent "$AGENT" -- sh -c '
    mkdir -p ~/agent/runs && cd ~/agent/runs && cat > "$1.sh" || exit
    [ -s "$1.sh" ] || { echo "error: agent-bootstrap.sh arrived empty" >&2; exit 1; }
    setsid sh -c "sh ./\$1.sh run \"\$1\" \"\$2\" > \$1.log 2>&1; echo \$? > \$1.status; sh ./\$1.sh sleep 20 > /dev/null 2>&1" \
      sh "$1" "$2" < /dev/null > /dev/null 2>&1 &' sh "$RUN" "$(printf '%s' "$TASK" | base64 | tr -d '\n')" \
    < "$DIR/agent-bootstrap.sh"
  log "started on $AGENT (Ctrl-C stops following, not the run: ./agent.sh logs $RUN, ./agent.sh stop)"
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
  [ -n "$1" ] || die "no run on $AGENT yet: ./agent.sh migrate"
  run_id "$1"
  log "run $1"
  rc=0; follow "$1" || rc=$?
  # Woken to read a finished run: back to sleep (a run still going sleeps on its own).
  remote sleep
  [ $rc -eq 0 ] || die "run $1 failed (exit $rc); its sandbox, if any: ./agent.sh cleanup $1"
}

# purge: every run and the app's stack, deleted on the agent (the agent itself stays). The deploy
# layer is pushed first: it is the one that knows the stack's computers, databases and secrets.
purge() {
  if [ "${1:-}" != --yes ]; then
    [ -t 0 ] || die "purge needs confirmation: $0 purge --yes"
    printf 'Delete every run (sandboxes, checkpoints, logs) and %s with its databases (all data) and secrets? Type "purge": ' \
      "$BASE_COMPUTER"
    read -r answer
    [ "$answer" = purge ] || die "aborted"
  fi
  wake
  push_deploy
  remote purge
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
    log "now log Claude Code in once: nevia shell $AGENT, then claude and /login; then ./agent.sh migrate"
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
  migrate) migrate ;;
  setup)   setup "${2:-}" ;;
  ls)      remote ls ;;
  logs)    logs "${2:-}" ;;
  stop)    [ -z "${2:-}" ] || run_id "$2"; remote stop "${2:-}" ;;
  preview) run_id "${2:-}"; remote preview "$2" ;;
  cleanup) run_id "${2:-}"; remote cleanup "$2" ;;
  purge)   purge "${2:-}" ;;
  down)    down ;;
  destroy) destroy "${2:-}" ;;
esac
