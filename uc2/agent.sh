#!/bin/sh
# A Claude Code agent on Nevia changes an app and opens a PR: its migration from MongoDB to
# Postgres (migrate), or a feature you describe (task), tested on a sandbox first.
# Config: ../.env only (UC2_ lines first, see ../.env.example).
#
# Usage: ./agent.sh setup                create the agent, or renew its key
#        ./agent.sh deploy               deploy REPO only: the app and its MongoDB, no Claude, no PR
#        ./agent.sh migrate              run the migration of REPO
#        ./agent.sh task "<what to build>"   develop that in REPO (or: task -f <file>), test it on
#                                        a sandbox (checkpoint of the app, fork of its MongoDB)
#        ./agent.sh logs [<run>]         follow a run (default: the latest)
#        ./agent.sh report [<run>]       fetch a run's report (time, Claude's cost) into reports/
#        ./agent.sh stop [<run>]         stop a run (default: the one going)
#        ./agent.sh resume <run>         redo the sandbox and the PR of a run, on Claude's change
#        ./agent.sh ls                   list the sandboxes
#        ./agent.sh cleanup <run>        delete a sandbox
#        ./agent.sh purge [--yes]        delete every run, the app and its databases
#        ./agent.sh destroy [--yes]      delete the agent
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/deploy/lib.sh"
TAG=agent.sh
usage() { sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0"; }

CMD=${1:-}
case $CMD in
  setup|deploy|migrate|task|logs|report|stop|resume|ls|cleanup|purge|destroy) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 1 ;;
esac

# .env alone decides: the shell's values are dropped first.
unset NEVIA_WORKSPACE GITHUB_TOKEN REPO BASE_BRANCH CLAUDE_MODEL CLAUDE_EFFORT CLAUDE_MAX_TURNS CLAUDE_ALLOWED_TOOLS AGENT AGENT_KEY_TTL KEY_TTL APP_COMPUTER MIGRATION_DIR \
  ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
load_env "$DIR/../.env" UC2
# The computer, its Nevia key and the runs going on it: shared with uc1 and uc3 (AGENT).
. "$DIR/../common/computer.sh"
BASE_BRANCH=${BASE_BRANCH:-main}
BASE_COMPUTER=${APP_COMPUTER:-hello-app}
REPO=${REPO:-}
TASK_FILE=$DIR/tasks/mongo-to-postgres.txt
for c in nevia jq; do command -v $c >/dev/null || die "$c not found"; done
[ -n "${NEVIA_WORKSPACE:-}" ] || die "set NEVIA_WORKSPACE in .env"

# on_agent <nevia exec args>...: nevia exec on the agent, with the config from .env. Claude's
# gateway (ANTHROPIC_BASE_URL) and models go with each run; its token is a secret in the
# computer's env (setup).
on_agent() {
  nv exec -e "AGENT=$AGENT" -e "AGENT_NEVIA_WORKSPACE=$NEVIA_WORKSPACE" ${RUN_KIND:+-e "RUN_KIND=$RUN_KIND"} \
    -e "REPO=$REPO" -e "BASE_BRANCH=$BASE_BRANCH" -e "BASE_COMPUTER=$BASE_COMPUTER" \
    ${CLAUDE_MODEL:+-e "CLAUDE_MODEL=$CLAUDE_MODEL"} ${CLAUDE_EFFORT:+-e "CLAUDE_EFFORT=$CLAUDE_EFFORT"} ${CLAUDE_MAX_TURNS:+-e "CLAUDE_MAX_TURNS=$CLAUDE_MAX_TURNS"} \
    ${CLAUDE_ALLOWED_TOOLS:+-e "CLAUDE_ALLOWED_TOOLS=$CLAUDE_ALLOWED_TOOLS"} \
    ${ANTHROPIC_BASE_URL:+-e "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"} ${ANTHROPIC_MODEL:+-e "ANTHROPIC_MODEL=$ANTHROPIC_MODEL"} \
    ${ANTHROPIC_DEFAULT_OPUS_MODEL:+-e "ANTHROPIC_DEFAULT_OPUS_MODEL=$ANTHROPIC_DEFAULT_OPUS_MODEL"} \
    ${ANTHROPIC_DEFAULT_SONNET_MODEL:+-e "ANTHROPIC_DEFAULT_SONNET_MODEL=$ANTHROPIC_DEFAULT_SONNET_MODEL"} \
    ${ANTHROPIC_DEFAULT_HAIKU_MODEL:+-e "ANTHROPIC_DEFAULT_HAIKU_MODEL=$ANTHROPIC_DEFAULT_HAIKU_MODEL"} "$@"
}
# The script the agent runs: lib.sh, then agent-bootstrap.sh.
script() { cat "$DIR/deploy/lib.sh" "$DIR/agent-bootstrap.sh"; }

# wake: nevia exec never wakes a computer (exit 5); exit 6 (busy) is retried.
wake() {
  i=0
  while :; do
    rc=0; nv exec -n "$AGENT" -- true >/dev/null 2>&1 || rc=$?
    case $rc in
      0) return ;;
      5) say "waking $AGENT"; nv awake "$AGENT" --wait >/dev/null; return ;;
      3) die "no agent computer $AGENT: ./agent.sh setup" ;;
      6) i=$((i + 1)); [ $i -lt 12 ] || die "cannot reach $AGENT"; sleep 5 ;;
      *) die "cannot reach $AGENT (nevia exec exit $rc)" ;;
    esac
  done
}
remote() { wake; script | on_agent "$AGENT" -- sh -s "$@"; }
need_repo() { case $REPO in */*/*|/*|*/|*[!A-Za-z0-9_./-]*|'') die "set REPO=owner/name in .env" ;; */*) ;; *) die "set REPO=owner/name in .env" ;; esac; }
run_id() { case ${1:-} in ''|*[!0-9]*) die "which run? ./agent.sh ls" ;; esac; }
confirm() {
  [ "$2" = --yes ] && return
  [ -t 0 ] || die "$1 needs confirmation: $0 $1 --yes"
  printf '%s Type "%s": ' "$3" "$1"; read -r answer; [ "$answer" = "$1" ] || die "aborted"
}

# push_analysis <run>: what Claude reads of the mongo2pg analysis (schemas, stats, mappings, SQL),
# without the sample values they quote: those are real data and never leave this machine.
push_analysis() {
  tmp=$(mktemp -d)
  cp -R "$MIGRATION_DIR/source/collections" "$tmp/collections"
  [ ! -d "$MIGRATION_DIR/schema/tables" ] || cp -R "$MIGRATION_DIR/schema/tables" "$tmp/tables"
  # Nothing hidden (e.g. .claude/ left by a Claude Code session in that folder).
  find "$tmp" -name '.*' -prune -exec rm -rf {} +
  for f in $(find "$tmp" -name '*.json'); do jq 'del(.. | .values?)' "$f" > "$f.new" && mv "$f.new" "$f"; done
  # The examples of an infer warning: the lines under `examples:`, more indented or list items.
  for f in $(find "$tmp" -name '*.stats.yaml'); do
    awk '/^ *examples:/ { match($0, /^ */); ind = RLENGTH; skip = 1; next }
         skip { match($0, /^ */); if (RLENGTH > ind || substr($0, RLENGTH + 1, 2) == "- ") next; skip = 0 }
         { print }' "$f" > "$f.new" && mv "$f.new" "$f"
  done
  say "pushing the mongo2pg analysis of $MIGRATION_DIR (without its sample values)"
  push_dir "$AGENT" "agent/runs/$1.analysis" "$tmp" . || { rm -rf "$tmp"; die "could not push the analysis"; }
  rm -rf "$tmp"
}

# follow <run> [<from line>]: print the run's new log lines until it writes its status; returns it.
follow() {
  n=${2:-0} fails=0
  set -- "$1"
  while :; do
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
        case ${4:-} in '') ;; missing) die "no run $1 on $AGENT" ;; *) return "$4" ;; esac ;;
      5) die "$AGENT went to sleep: run $1 is over" ;;
      *) fails=$((fails + 1)); [ $fails -lt 12 ] || die "lost contact with $AGENT: ./agent.sh logs $1" ;;
    esac
    sleep 5
  done
}

# fetch_report <run>: the report the run wrote on the agent, into reports/<run>.md.
fetch_report() {
  mkdir -p "$DIR/reports"
  if nv exec -n "$AGENT" -- sh -c 'cat ~/agent/runs/$1.report.md' sh "$1" > "$DIR/reports/$1.md.new" 2>/dev/null &&
     [ -s "$DIR/reports/$1.md.new" ]; then
    mv "$DIR/reports/$1.md.new" "$DIR/reports/$1.md"
    cost=$(sed -n 's/^- cost: \*\*\([^*]*\)\*\*.*/\1/p' "$DIR/reports/$1.md" | head -n 1)
    total=$(awk -F '|' '/\*\*total\*\*/ { gsub(/[ *]/, "", $3); print $3 }' "$DIR/reports/$1.md")
    say "report: reports/$1.md (${total:-?}${cost:+, Claude $cost})"
  else
    rm -f "$DIR/reports/$1.md.new"
    say "warning: no report for run $1 on $AGENT"
  fi
}

# going: the run going on the agent, if any.
going() {
  nv exec "$AGENT" -- sh -c 'for f in ~/agent/runs/*.pid; do
      [ -e "$f" ] && [ ! -e "${f%.pid}.status" ] && kill -0 "$(cat "$f")" 2>/dev/null && basename "$f" .pid
    done; true'
}

# start <run> <command> [<arg>]: a run outlasts a nevia exec, so the script is stored on the agent
# and started detached; it appends to <run>.log, then writes <run>.status and puts the agent to
# sleep. Then follows it.
start() {
  # Prints the log's current length: following starts after it (resume appends to the run's log).
  n=$(script | on_agent "$AGENT" -- sh -c '
    mkdir -p ~/agent/runs && cd ~/agent/runs && cat > "$1.sh" && [ -s "$1.sh" ] || exit 1
    rm -f "$1.status"
    if [ -f "$1.log" ]; then wc -l < "$1.log"; else echo 0; fi
    setsid sh -c "sh ./\$1.sh \$2 \"\$1\" \"\$3\" >> \$1.log 2>&1; echo \$? > \$1.status; sh ./\$1.sh sleep 20 > /dev/null 2>&1" \
      sh "$1" "$2" "$3" < /dev/null > /dev/null 2>&1 &' sh "$1" "$2" "${3:-}") || die "could not start run $1 on $AGENT"
  say "started (Ctrl-C stops following, not the run: ./agent.sh logs $1, ./agent.sh stop)"
  rc=0; follow "$1" "$(printf '%s' "$n" | tr -d ' ')" || rc=$?
  fetch_report "$1"
  [ $rc -eq 0 ] || [ "${RUN_KIND:-}" != deploy ] || die "run $1 failed (exit $rc): ./agent.sh deploy again once fixed"
  [ $rc -eq 0 ] || die "run $1 failed (exit $rc): ./agent.sh resume $1 redoes its sandbox and PR, ./agent.sh cleanup $1 deletes it"
}

resume() {
  run_id "${1:-}"
  wake
  going=$(going)
  [ -z "$going" ] || die "run $going is still going: ./agent.sh logs $going"
  say "pushing the deploy layer"
  push_dir "$AGENT" agent/deploy "$DIR/deploy" .
  say "resuming run $1 (sandbox and PR, on the change Claude left)"
  start "$1" resume
}

# deploy: REPO's BASE_BRANCH on BASE_COMPUTER and its database (data kept), checked; nothing else.
deploy() {
  [ -z "${2:-}" ] || die "deploy takes no argument: set REPO in .env"
  need_repo
  wake
  going=$(going)
  [ -z "$going" ] || die "run $going is still going: ./agent.sh logs $going"
  say "pushing the deploy layer"
  push_dir "$AGENT" agent/deploy "$DIR/deploy" .
  RUN=$(date -u +%y%m%d%H%M%S)
  say "run $RUN: deploying $REPO ($BASE_BRANCH) on $BASE_COMPUTER"
  RUN_KIND=deploy start "$RUN" run
}

migrate() {
  [ -z "${2:-}" ] || die "migrate takes no argument: set REPO in .env"
  need_repo
  MIGRATION_DIR=${MIGRATION_DIR:-$DIR/migration/${REPO##*/}}
  [ -d "$MIGRATION_DIR/source/collections" ] || die "no mongo2pg analysis in $MIGRATION_DIR (README.md: mongo2pg infer)"
  wake
  going=$(going)
  [ -z "$going" ] || die "run $going is still going: ./agent.sh logs $going"
  say "pushing the deploy layer"
  push_dir "$AGENT" agent/deploy "$DIR/deploy" .
  RUN=$(date -u +%y%m%d%H%M%S)
  say "run $RUN: $REPO ($BASE_BRANCH)"
  push_analysis "$RUN"
  start "$RUN" run "$(base64 < "$TASK_FILE" | tr -d '\n')"
}

# task "<what to build>" | -f <file>: a feature for REPO, wrapped in tasks/feature.txt. No analysis
# and no Postgres: the app stays on its database; the sandbox tests it on a fork of its data.
task() {
  case ${2:-} in
    -f) [ -s "${3:-}" ] || die "task -f <file>: no file '${3:-}'"; text=$(cat "$3") ;;
    *) text=${2:-} ;;
  esac
  [ -n "$(printf '%s' "$text" | tr -d '[:space:]')" ] || die 'what to build? ./agent.sh task "<the feature>" (or task -f <file>)'
  need_repo
  wake
  going=$(going)
  [ -z "$going" ] || die "run $going is still going: ./agent.sh logs $going"
  say "pushing the deploy layer"
  push_dir "$AGENT" agent/deploy "$DIR/deploy" .
  RUN=$(date -u +%y%m%d%H%M%S)
  say "run $RUN: a task on $REPO ($BASE_BRANCH)"
  # The request in the prompt as it is ({{TASK}} replaced by awk, which takes it as data).
  prompt=$(TASK_TEXT=$text awk '$0 == "{{TASK}}" { print ENVIRON["TASK_TEXT"]; next } { print }' "$DIR/tasks/feature.txt")
  RUN_KIND=task start "$RUN" run "$(printf '%s\n' "$prompt" | base64 | tr -d '\n')"
}

logs() {
  wake
  set -- "${1:-$(nv exec -n "$AGENT" -- sh -c 'ls -t ~/agent/runs/*.log 2>/dev/null | head -n 1 | xargs -r basename -s .log')}"
  run_id "$1"
  say "run $1"
  rc=0; follow "$1" || rc=$?
  fetch_report "$1"
  remote sleep
  [ $rc -eq 0 ] || die "run $1 failed (exit $rc); its sandbox, if any: ./agent.sh cleanup $1"
}

# report [<run>]: a run's report (default: the latest), fetched again.
report() {
  wake
  set -- "${1:-$(nv exec -n "$AGENT" -- sh -c 'ls -t ~/agent/runs/*.report.md 2>/dev/null | head -n 1 | xargs -r basename -s .report.md')}"
  run_id "$1"
  fetch_report "$1"
  remote sleep
}

# setup: the GitHub token into a workspace secret, then the computer shared with uc1 and uc3, with
# its Nevia key (made only if there is none: agent/common/computer.sh), Claude Code's credential
# and the token in its env. Run it again after changing a token in .env.
setup() {
  case ${GITHUB_TOKEN:-} in github_pat_*|ghp_*) ;; *) die "set GITHUB_TOKEN in .env (a GitHub token, github_pat_…)" ;; esac
  printf '%s' "$GITHUB_TOKEN" | nv --yes secrets GITHUB_TOKEN --file - >/dev/null
  agent_setup -e "GITHUB_TOKEN={{secrets.GITHUB_TOKEN}}"
  say "agent ready"
}

# destroy [--yes]: what is uc2's on the computer (~/agent: the runs, the clone, the tools) and the
# GitHub token. The computer, its key and Claude Code's secrets are shared with uc1 and uc3:
# ../computer.sh destroy deletes them; the sandboxes and the app: ./agent.sh purge.
destroy() {
  confirm destroy "${1:-}" "Delete ~/agent on $AGENT and the secret GITHUB_TOKEN?"
  if [ -n "$(status_json "$AGENT")" ]; then
    wake
    going=$(going)
    [ -z "$going" ] || die "run $going is still going: ./agent.sh stop first"
    say "deleting ~/agent on $AGENT"
    nv exec -n "$AGENT" -- sh -c 'chmod -R u+w ~/agent 2>/dev/null; rm -rf ~/agent'
  fi
  ! agent_has_secret GITHUB_TOKEN || { say "deleting secret GITHUB_TOKEN"; nv --yes secrets unset GITHUB_TOKEN >/dev/null; }
  say "kept: computer $AGENT, its key and Claude Code's secrets (../computer.sh destroy), the sandboxes and the app (./agent.sh purge)"
}

# In braces, with exit: sh reads a script as it runs it, so this is all read before a run starts
# and an edit of this file during a run (35 min) can't break its end.
{
case $CMD in
  setup)   setup ;;
  deploy)  deploy "$@" ;;
  migrate) migrate "$@" ;;
  task)    task "$@" ;;
  logs)    logs "${2:-}" ;;
  report)  report "${2:-}" ;;
  stop)    [ -z "${2:-}" ] || run_id "$2"; remote stop "${2:-}" ;;
  resume)  resume "${2:-}" ;;
  ls)      remote ls ;;
  cleanup) run_id "${2:-}"; remote cleanup "$2" ;;
  purge)
    confirm purge "${2:-}" "Delete every run and $BASE_COMPUTER with its databases (all data) and secrets?"
    wake; push_dir "$AGENT" agent/deploy "$DIR/deploy" .; remote purge ;;
  destroy) destroy "${2:-}" ;;
esac
exit
}
