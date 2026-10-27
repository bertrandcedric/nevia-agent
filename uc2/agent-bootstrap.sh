#!/bin/sh
# Runs ON the agent computer (deploy/lib.sh + this, sent by agent.sh through nevia exec):
#   sh -s ls | cleanup <run> | stop [<run>] | purge | sleep [<delay>]
#   sh <run>.sh run <run> <task-b64> | resume <run>    stored in ~/agent/runs, detached; each
#   writes runs/<run>.report.md at its end, failed or not (agent.sh brings it to uc2/reports)
# Every command but run and resume ends by putting the agent to sleep, unless a run is going.
#
# run: 1. clone the repo  2-3. recreate the app and its database (deploy layer), check them
#      4. Claude changes the code (no Nevia key, no git, no GitHub): RUN_KIND=migrate (default),
#         its migration to Postgres, with a Postgres of its own; RUN_KIND=task, the feature the
#         task asks for, and a check of it (the feature check)
#      5. a sandbox (checkpoint of the app + a new database, or a branch of it, a fork of its data,
#         when the app keeps its database kind) tests the change, and runs the feature check
#      6. push, open the PR.
#      RUN_KIND=deploy: steps 1-2 only (the stack deployed, its data kept), no Claude, no PR.
# resume: steps 5 and 6 again, on the change the run's Claude left in the clone.
# Env: GITHUB_TOKEN, AGENT_NEVIA_KEY (the computer's), AGENT, AGENT_NEVIA_WORKSPACE, REPO,
# BASE_BRANCH, BASE_COMPUTER, CLAUDE_MODEL, CLAUDE_EFFORT, CLAUDE_MAX_TURNS, CLAUDE_ALLOWED_TOOLS (from agent.sh),
# and with a gateway ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL, ANTHROPIC_DEFAULT_*_MODEL,
# which Claude Code reads itself (CLAUDE_MODEL, if set, wins over ANTHROPIC_MODEL).
set -eu
TAG=agent
BASE=$HOME/agent
mkdir -p "$BASE/log" "$BASE/tmp" "$BASE/runs"
CMD=${1:-}
BASE_BRANCH=${BASE_BRANCH:-main}
BASE_COMPUTER=${BASE_COMPUTER:-hello-app}

# Timing: each step shows the time since the start and how long the previous one took.
START=$(date +%s) STEP_START= STEP_NAME=
dur() { printf '%dm%02ds' $(($1 / 60)) $(($1 % 60)); }
# A run's steps also go to STEPS (seconds, name), for its report.
step_done() {
  [ -n "$STEP_NAME" ] || return 0
  d=$(($(date +%s) - STEP_START))
  say "⏱ $STEP_NAME: $(dur $d)"
  [ -z "${STEPS:-}" ] || printf '%s\t%s\n' "$d" "$STEP_NAME" >> "$STEPS"
}
step() {
  step_done
  STEP_NAME=$*; STEP_START=$(date +%s)
  printf '\n\033[1m[agent] ─── %s ─── +%s\033[0m\n' "$*" "$(dur $((STEP_START - START)))"
}
run_done() { step_done; STEP_NAME=; say "⏱ run: $(dur $(($(date +%s) - START)))"; }
die() {
  echo "[agent] error: $*" >&2
  [ -z "$STEP_NAME" ] || echo "[agent] ⏱ failed during \"$STEP_NAME\" after $(dur $(($(date +%s) - START)))" >&2
  exit 1
}

for v in AGENT AGENT_NEVIA_WORKSPACE AGENT_NEVIA_KEY; do
  eval "[ -n \"\${$v:-}\" ]" || die "missing env $v: ./agent.sh setup"
done

# The agent's Nevia key: in a variable children don't inherit, written to a key file (this user
# only) while this script needs nevia, never while Claude runs.
NEVIA_KEY=$AGENT_NEVIA_KEY
unset AGENT_NEVIA_KEY
for f in "$BASE"/tmp/nevia-key.*; do [ -e "$f" ] && ! kill -0 "${f##*.}" 2>/dev/null && rm -f "$f"; done
KEY_FILE=$BASE/tmp/nevia-key.$$
key_on() { (umask 077 && printf '%s\n' "$NEVIA_KEY" > "$KEY_FILE"); }
key_off() { rm -f "$KEY_FILE"; }
export NEVIA_KEY_FILE=$KEY_FILE NEVIA_WORKSPACE=$AGENT_NEVIA_WORKSPACE
trap key_off EXIT
key_on
# nevia is not on the image: the official installer (checksum checked) puts it in ~/.local/bin.
if ! command -v nevia >/dev/null; then
  say "installing the Nevia CLI"
  curl -fsSL https://get.nevia.cloud | sh >/dev/null || die "could not install the Nevia CLI"
  export PATH=$HOME/.local/bin:$PATH
fi
# The key is the computer's, shared with uc1 and uc3 (agent/common/computer.sh): its expiry, then
# whether Nevia takes it (a rotation revokes the one before).
case ${AGENT_NEVIA_KEY_EXPIRES:-} in
  ''|*[!0-9]*) ;;
  *) left=$((AGENT_NEVIA_KEY_EXPIRES - $(date +%s)))
     [ "$left" -gt 0 ] || die "the agent's Nevia key has expired: agent/computer.sh rotate-key"
     [ "$left" -gt 259200 ] || say "warning: the agent's Nevia key expires in $((left / 3600)) h: agent/computer.sh rotate-key, once no run is going" ;;
esac
nv whoami >/dev/null || die "the agent's Nevia key is refused (expired, or rotated since this computer got it?): agent/computer.sh rotate-key"

# A run's sandbox: <app>-<run> and whatever deploy.sh named <name>-<run> or <NAME>_<run>.
names() {
  case ${1:-} in ''|*[!0-9]*) die "a run id is digits only, got '${1:-}'" ;; esac
  CHECKPOINT=agent-$1 SANDBOX_COMPUTER=$BASE_COMPUTER-$1
}
# going_run: the run going, if any (a live process with no status yet).
going_run() {
  for f in "$BASE"/runs/*.pid; do
    [ -e "$f" ] && [ ! -e "${f%.pid}.status" ] && kill -0 "$(cat "$f")" 2>/dev/null && basename "$f" .pid
  done; true
}

# busy_runs: the runs going on the computer, of any use case (~/.busy/<uc>-<id>, a live pid each;
# agent/common/computer.sh): uc1 and uc3 run on it too.
busy_runs() {
  for f in "$HOME"/.busy/*; do [ -e "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null && basename "$f"; done; true
}

# sleep_if_idle [<delay>]: put the agent to sleep in <delay> s (3 by default, so the calling exec
# returns first), unless a run of any use case is going then. One pending sleep at a time, dropped
# if the agent slept meanwhile (the clock jumped).
sleep_if_idle() {
  busy=$(busy_runs | tr '\n' ' ')
  [ -z "$busy" ] || { say "runs going ($busy): $AGENT stays awake"; return 0; }
  say "putting $AGENT to sleep in ${1:-3} s"
  id=$(status_json "$AGENT" | jq -r '.computer.id // empty')
  [ -n "$id" ] || die "no id for $AGENT"
  [ ! -f "$BASE/tmp/sleep.pid" ] || kill "$(cat "$BASE/tmp/sleep.pid")" 2>/dev/null || true
  # The token first, while the key file exists: this script deletes it as soon as it exits.
  token=$(nv auth access-token)
  [ -n "$token" ] || die "no Nevia access token"
  export BASE
  printf '%s\n' "$token" | setsid sh -c '
    echo $$ > "$BASE/tmp/sleep.pid"
    read -r t; t0=$(date +%s); sleep "$2"
    [ $(($(date +%s) - t0)) -le $(($2 + 5)) ] || exit 0
    for f in "$HOME"/.busy/*; do
      [ -e "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null && exit 0
    done
    curl -fsS --max-time 20 -X POST -H "Authorization: Bearer $t" "https://api.nevia.cloud/v1/computers/$1/sleep"' \
    sh "$id" "${1:-3}" > /dev/null 2>&1 &
}

# delete_run <run> <computers> <databases> <secrets>: what a run left, from those lists; the
# sandbox's app goes first (a checkpoint can't be deleted while a computer restored from it lives).
delete_run() {
  names "$1"
  for c in $(printf '%s\n' "$2" | grep -x "$SANDBOX_COMPUTER"; printf '%s\n' "$2" | grep -- "-$1\$" | grep -vx "$SANDBOX_COMPUTER"); do
    say "deleting computer $c"; nv --yes rm "$c" --wait >/dev/null
  done
  for d in $(printf '%s\n' "$3" | grep -- "-$1\$"); do say "deleting database $d"; nv --yes pg rm "$d" --wait >/dev/null; done
  for s in $(printf '%s\n' "$4" | grep -- "_$1\$"); do say "deleting secret $s"; nv --yes secrets unset "$s" >/dev/null; done
  rc=0; nv --yes checkpoint rm -c "$BASE_COMPUTER" "$CHECKPOINT" >/dev/null 2>&1 || rc=$?
  case $rc in 0) say "deleted checkpoint $CHECKPOINT" ;; 3) ;; *) die "nevia checkpoint rm $CHECKPOINT failed (exit $rc)" ;; esac
  [ ! -d "$BASE/runs/$1.analysis" ] || { chmod -R u+w "$BASE/runs/$1.analysis"; rm -rf "$BASE/runs/$1.analysis"; }
  rm -rf "$BASE/work/$1"
}
# The lists, read once (Nevia rate-limits its calls).
read_lists() {
  computers=$(nv --json ls | jq -r '.computers[].name')
  databases=$(nv --json pg ls | jq -r '[.. | objects | select(has("name")) | .name] | unique[]')
  secrets=$(nv --json secrets ls | jq -r '.secrets[].name')
}

case $CMD in
  sleep) sleep_if_idle "${2:-3}"; exit 0 ;;
  ls)
    nv --json ls | jq -r --arg p "$BASE_COMPUTER-" '
      .computers[] | select(.name | startswith($p)) | select(.name | ltrimstr($p) | test("^[0-9]+$"))
      | "\(.name | ltrimstr($p))\t\(.name)\t\(.state)\t\(first(.ports[]?.url) // "-")"' |
      { printf 'RUN\tCOMPUTER\tSTATE\tURL\n'; cat; }
    sleep_if_idle; exit 0 ;;
  cleanup)
    read_lists; delete_run "${2:-}" "$computers" "$databases" "$secrets"; say "run $2 cleaned up"
    sleep_if_idle; exit 0 ;;
  stop)
    # Stops the run's processes but its session leader, which then writes the status (143) and
    # puts the agent to sleep, as after any run.
    r=${2:-$(going_run)}
    [ -n "$r" ] || { say "no run is going"; sleep_if_idle; exit 0; }
    [ "$r" = "$(going_run)" ] || die "run $r is not going"
    leader=$(cut -d' ' -f5 "/proc/$(cat "$BASE/runs/$r.pid")/stat")
    group() {
      for f in /proc/[0-9]*/stat; do
        set -- $(cat "$f" 2>/dev/null) && [ "${5:-}" = "$leader" ] && [ "$1" != "$leader" ] && echo "$1"
      done; true
    }
    echo "[agent] stopped by ./agent.sh stop" >> "$BASE/runs/$r.log"
    say "stopping run $r"
    kill -TERM $(group) 2>/dev/null || true
    i=0; while [ -n "$(group)" ] && [ $i -lt 10 ]; do sleep 1; i=$((i + 1)); done
    [ -z "$(group)" ] || kill -KILL $(group) 2>/dev/null || true
    say "run $r stopped; what it created: ./agent.sh cleanup $r"
    exit 0 ;;
  purge)
    # Every run, then the app's stack (deploy.sh destroy). The agent stays.
    [ -z "$(going_run)" ] || die "a run is going: ./agent.sh stop first"
    read_lists
    runs=$( { ls "$BASE/runs" 2>/dev/null | sed -n 's/^\([0-9][0-9]*\)\..*/\1/p'
              printf '%s\n' "$computers" | sed -n "s/^$BASE_COMPUTER-\([0-9][0-9]*\)\$/\1/p"; } | sort -u)
    for r in $runs; do delete_run "$r" "$computers" "$databases" "$secrets"; done
    COMPUTER=$BASE_COMPUTER "$BASE/deploy/deploy.sh" destroy --yes
    for d in runs repo analysis work; do [ ! -e "$BASE/$d" ] || { chmod -R u+w "$BASE/$d"; rm -rf "$BASE/$d"; }; done
    say "purged: $(echo $runs | wc -w | tr -d ' ') run(s) and the stack of $BASE_COMPUTER"
    sleep_if_idle; exit 0 ;;
  run|resume) ;;
  *) die "unknown command '$CMD'" ;;
esac

# --- run <run> <task-b64> | resume <run> ---------------------------------------------
RUN=${2:-}
names "$RUN"
for v in GITHUB_TOKEN REPO; do eval "[ -n \"\${$v:-}\" ]" || die "missing env $v"; done
[ -z "$(going_run)" ] || die "another run is going: ./agent.sh logs"
echo $$ > "$BASE/runs/$RUN.pid"
# In the computer's register of runs while it goes (any use case: agent/common/computer.sh): nothing
# puts the computer to sleep or rotates the key under it.
mkdir -p "$HOME/.busy" && BUSY=$HOME/.busy/uc2-$RUN && echo $$ > "$BUSY"
STEPS=$BASE/runs/$RUN.steps REPORT=$BASE/runs/$RUN.report.md
[ "$CMD" = resume ] || rm -f "$STEPS" "$BASE/runs/$RUN.result.json"
: >> "$STEPS"

# report <exit code>: runs/<run>.report.md, its outcome, the time of each step (a resume's added to
# the run's), what Claude cost and what the run left on Nevia. The Nevia CLI gives no prices.
report() {
  [ -z "$STEP_NAME" ] || printf '%s\t%s (%s)\n' "$(($(date +%s) - STEP_START))" "$STEP_NAME" \
    "$([ "$1" = 143 ] && echo stopped || echo failed)" >> "$STEPS"
  key_on
  live=$( { nv --json ls | jq -r --arg s "-$RUN" '.computers[] | select(.name | endswith($s)) | "computer `\(.name)` (\(.state))"'
            nv --json pg ls | jq -r --arg s "-$RUN" '[.. | objects | select(has("name")) | .name] | unique[] | select(endswith($s)) | "database `\(.)`"'
            nv --json checkpoint ls | jq -r --arg c "$CHECKPOINT" '[.. | objects | select(.name? == $c)] | first | select(.) | "checkpoint `\(.name)`"'
          } 2>/dev/null) || live="(could not list them)"
  case $1 in 0) outcome=ok ;; 143) outcome=stopped ;; *) outcome="failed (exit $1)" ;; esac
  {
    printf '# Run %s\n\n' "$RUN"
    printf -- '- %s of `%s` (`%s`), stack `%s`, on `%s`\n' "${RUN_KIND:-migrate}" "$REPO" "$BASE_BRANCH" "$BASE_COMPUTER" "$AGENT"
    printf -- '- %s, %s: %s\n' "$CMD" "$(date -u -d "@$START" '+%Y-%m-%d %H:%M UTC')" "$outcome"
    [ -z "${PR_URL:-}" ] || printf -- '- pull request: %s\n' "$PR_URL"
    [ -z "${SANDBOX_URL:-}" ] || printf -- '- preview: %s\n' "$SANDBOX_URL"
    [ -z "${APP_URL:-}" ] || printf -- '- app: %s\n' "$APP_URL"
    printf '\n## Time\n\n| Step | Duration |\n|---|---:|\n'
    awk -F '\t' '{ t += $1; printf "| %s | %dm%02ds |\n", $2, $1 / 60, $1 % 60 }
      END { printf "| **total** | **%dm%02ds** |\n", t / 60, t % 60 }' "$STEPS"
    printf '\n## Claude Code\n\n'
    if [ -s "$BASE/runs/$RUN.result.json" ]; then
      jq -r 'def usd: "$\((. // 0) * 10000 | round / 10000)";
        "- cost: **\(.total_cost_usd | usd)** (Claude Code'"'"'s estimate, at list prices: a gateway bills its own)",
        "- \(.num_turns // "?") turns, \(((.duration_ms // 0) / 1000) | floor) s (API \(((.duration_api_ms // 0) / 1000) | floor) s)\(if .is_error then ", ended with \(.subtype)" else "" end)",
        "", "| Model | Input | Output | Cache read | Cache write | Cost |", "|---|---:|---:|---:|---:|---:|",
        (.modelUsage // {} | to_entries[] | .value as $u |
          "| `\(.key)` | \($u.inputTokens // 0) | \($u.outputTokens // 0) | \($u.cacheReadInputTokens // 0) | \($u.cacheCreationInputTokens // 0) | \($u.costUSD | usd) |")' \
        "$BASE/runs/$RUN.result.json"
    elif [ "${RUN_KIND:-migrate}" = deploy ]; then
      echo "Not run (deploy)."
    else
      echo "No result: Claude did not finish (stopped, or the run failed before)."
    fi
    printf '\n## Nevia\n\nNo prices from the Nevia CLI: the time above is the agent computer'"'"'s (and the stack'"'"'s), '
    printf 'to price at your plan'"'"'s rates. Left by the run (`./agent.sh cleanup %s` deletes them):\n\n' "$RUN"
    if [ -n "$live" ]; then printf '%s\n' "$live" | sed 's/^/- /'; else echo "- nothing"; fi
  } > "$REPORT"
}
trap 'rc=$?; report "$rc" || say "warning: no report"; key_off; rm -f "$BUSY"' EXIT
# ./agent.sh stop: the EXIT trap still writes the report.
trap 'exit 143' TERM INT
ANALYSIS=$BASE/analysis
# A task's: Claude's directory outside the clone, and the feature check it writes there.
WORK=$BASE/work/$RUN
CHECK=$WORK/feature-check.sh
DEPLOY=$BASE/deploy/deploy.sh
# What resume needs from the run: the clone it worked in, the stack's database kind, the PR text.
STATE=$BASE/runs/$RUN.state
DEV_DB=$BASE_COMPUTER-db-$RUN

step "installing tools (first run only)"
install_java "$BASE"
# psql loads local/postgres-init into the sandbox, and Claude uses it (a system package: checked at each run).
if ! command -v psql >/dev/null; then
  say "installing the Postgres client"
  sudo -n apt-get -qq update >/dev/null && sudo -n env DEBIAN_FRONTEND=noninteractive \
    apt-get -qq install -y --no-install-recommends postgresql-client >/dev/null || die "could not install postgresql-client"
fi
[ "${RUN_KIND:-}" = deploy ] || command -v claude >/dev/null || die "Claude Code not found on this computer"
say "$(psql --version), Claude Code $(claude --version 2>/dev/null)"

# git with the token from the env, never written into .git/config.
git_() {
  git -c credential.helper= \
      -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' "$@"
}
app_db_kind() {
  sed -n 's/^[[:space:]]*database\.kind[[:space:]]*[=:][[:space:]]*//p' src/main/resources/application.properties 2>/dev/null |
    tail -1 | tr -d '[:space:]'
}
# url_decode <text>: %XX decoded (libpq's PG* variables take the raw user and password).
url_decode() {
  printf '%s' "$1" | awk '{ s = $0; o = ""; h = "0123456789abcdef"
    while (match(s, /%[0-9A-Fa-f][0-9A-Fa-f]/)) {
      c = tolower(substr(s, RSTART + 1, 2))
      o = o substr(s, 1, RSTART - 1) sprintf("%c", (index(h, substr(c, 1, 1)) - 1) * 16 + index(h, substr(c, 2, 1)) - 1)
      s = substr(s, RSTART + 3)
    }
    printf "%s", o s }'
}
# logged <log> <cmd>...: run cmd, show and save its output, return its exit code.
logged() {
  log=$1; shift
  { if "$@"; then rc=0; else rc=$?; fi; echo "$rc" > "$BASE/tmp/rc"; } 2>&1 | tee "$log"
  return "$(cat "$BASE/tmp/rc")"
}

if [ "$CMD" = run ]; then
  RUN_KIND=${RUN_KIND:-migrate}
  case $RUN_KIND in migrate|task|deploy) ;; *) die "RUN_KIND is migrate, task or deploy, got '$RUN_KIND'" ;; esac
  TASK=$(printf '%s' "${3:-}" | base64 -d | sed "s|{{CHECK}}|$CHECK|g")
  [ "$RUN_KIND" = deploy ] || [ -n "$TASK" ] || die "no task: agent.sh migrate or task sends it"
  [ "$RUN_KIND" != migrate ] || [ -d "$BASE/runs/$RUN.analysis" ] || die "no mongo2pg analysis for run $RUN: agent.sh migrate sends it"

  step "1/6 cloning $REPO ($BASE_BRANCH)"
  rm -rf "$BASE/repo"
  git_ clone -q --branch "$BASE_BRANCH" "https://github.com/$REPO.git" "$BASE/repo"
  cd "$BASE/repo"
  git config user.name "nevia-agent"
  git config user.email "nevia-agent@users.noreply.github.com"
  STACK_DB_KIND=$(app_db_kind)
  [ -f pom.xml ] && [ -n "$STACK_DB_KIND" ] || die "$REPO is not a Maven app with a database.kind"
  # Fail now rather than after Claude's work: a dry-run push checks the write access.
  [ "$RUN_KIND" = deploy ] || git_ push -q --dry-run origin "HEAD:refs/heads/agent/$RUN" 2>/dev/null ||
    die "GITHUB_TOKEN can't push to $REPO: give it Contents and Pull requests: Read and write"

  step "2/6 launching the app on $BASE_COMPUTER, 3/6 testing it"
  # deploy: the stack updated from the current branch, its data kept.
  if [ "$RUN_KIND" = deploy ]; then
    COMPUTER=$BASE_COMPUTER logged "$BASE/log/base.log" "$DEPLOY" deploy || die "deploying $BASE_BRANCH failed"
    APP_URL=$(json_field "$(status_json "$BASE_COMPUTER")" url)
    say "app: $APP_URL"
    run_done; exit 0
  fi
  # Recreated at each run, from the current branch: never an older version.
  COMPUTER=$BASE_COMPUTER logged "$BASE/log/base.log" "$DEPLOY" recreate || die "deploying $BASE_BRANCH failed: no PR"

  step "4/6 Claude Code on the $([ "$RUN_KIND" = task ] && echo task || echo migration)"
  if [ "$RUN_KIND" = migrate ]; then
    [ ! -e "$ANALYSIS" ] || chmod -R u+w "$ANALYSIS"
    rm -rf "$ANALYSIS" && cp -R "$BASE/runs/$RUN.analysis" "$ANALYSIS" && chmod -R a-w "$ANALYSIS"
    DIRS=$ANALYSIS
  else
    rm -rf "$WORK" && mkdir -p "$WORK"
    DIRS=$WORK
  fi
  if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    say "Claude Code through the gateway ${ANTHROPIC_BASE_URL:-(no ANTHROPIC_BASE_URL)}, model ${CLAUDE_MODEL:-${ANTHROPIC_MODEL:-the default}}"
  else
    [ -s "$HOME/.claude/.credentials.json" ] || say "warning: no Claude Code login: nevia shell $AGENT, then claude and /login (or ANTHROPIC_AUTH_TOKEN in .env)"
  fi
  set --
  if [ "$RUN_KIND" = migrate ]; then
    # Claude's own Postgres on Nevia, empty, named <app>-db-<run> (cleanup finds it by that suffix).
    # It gets it through the env only, as the app does on Nevia (DATABASE_URL, PG_CA_FILE) and as
    # psql reads it (PG*): the password never appears in a command, so never in the log.
    say "creating $DEV_DB, Claude's Postgres"
    nv pg new "$DEV_DB" --wait >/dev/null
    uri=$(nv pg connect "$DEV_DB" --uri)
    [ -n "$uri" ] || die "no URI for $DEV_DB"
    nv pg connect "$DEV_DB" --ca "$BASE/tmp/dev-ca.pem" >/dev/null
    rest=${uri#*://} creds=${uri#*://}; creds=${creds%%@*}; rest=${rest#*@}
    hostport=${rest%%/*} db=${rest#*/}
    case $hostport in *:*) pg_port=${hostport##*:} ;; *) pg_port=5432 ;; esac
    set -- DATABASE_URL="$uri" PG_CA_FILE="$BASE/tmp/dev-ca.pem" \
      PGHOST="${hostport%%:*}" PGPORT="$pg_port" PGUSER="$(url_decode "${creds%%:*}")" PGPASSWORD="$(url_decode "${creds#*:}")" \
      PGDATABASE="${db%%\?*}" PGSSLMODE=verify-full PGSSLROOTCERT="$BASE/tmp/dev-ca.pem"
  fi
  # The tools Claude may use without asking; read-only shell commands (cat, grep, find...) pass anyway.
  ALLOWED=${CLAUDE_ALLOWED_TOOLS:-Read,Edit,Write,Glob,Grep,KillShell,Bash(pkill:*),Bash(mvn:*),Bash(java:*),Bash(jar:*),Bash(curl:*),Bash(psql:*),Bash(pg_dump:*),Bash(diff:*),Bash(sleep:*),Bash(echo:*),Bash(unzip:*),Bash(kill:*)}
  # Without the GitHub token or the Nevia key; its events printed one line each as they come.
  key_off
  { env -u GITHUB_TOKEN -u NEVIA_KEY_FILE -u NEVIA_WORKSPACE "$@" \
      claude -p "$TASK" --output-format stream-json --verbose --max-turns "${CLAUDE_MAX_TURNS:-150}" \
        --permission-mode acceptEdits --tools "Read,Edit,Write,Glob,Grep,Bash,KillShell" --allowedTools "$ALLOWED" \
        ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} ${CLAUDE_EFFORT:+--effort "$CLAUDE_EFFORT"} \
        --add-dir "$DIRS" --add-dir /tmp \
        2>> "$BASE/log/claude.log" || tail -20 "$BASE/log/claude.log" >&2; } |
    tee "$BASE/tmp/stream.jsonl" |
    jq -nR --unbuffered -r '
      def short: tostring | split("\n")[0] | .[0:160];
      inputs | fromjson? |
      if .type == "assistant" then
        .message.content[]? |
        if .type == "tool_use" then "[claude] \(.name) \(.input | .file_path // .pattern // .command // .path // "" | short)"
        elif .type == "text" and (.text | test("\\S")) then "[claude] » \(.text | short)"
        else empty end
      elif .type == "user" then
        .message.content[]? | select(type == "object" and .type == "tool_result" and .is_error == true) |
        "[claude]   ✗ \(.content | if type == "array" then (map(.text? // "") | join(" ")) else . end | short)"
      else empty end' || true
  key_on
  set --
  unset uri rest creds
  if [ "$RUN_KIND" = migrate ]; then
    say "deleting $DEV_DB"
    nv --yes pg rm "$DEV_DB" --wait >/dev/null || say "warning: $DEV_DB not deleted (./agent.sh cleanup $RUN)"
    rm -f "$BASE/tmp/dev-ca.pem"
  fi
  jq -cR 'fromjson? | select(.type == "result")' "$BASE/tmp/stream.jsonl" | tail -n 1 > "$BASE/tmp/result.json" || true
  [ ! -s "$BASE/tmp/result.json" ] || cp "$BASE/tmp/result.json" "$BASE/runs/$RUN.result.json"
  [ -s "$BASE/tmp/result.json" ] || { tail -20 "$BASE/log/claude.log" >&2; die "Claude Code ended without a result: no PR"; }
  # The PR text: the title on the first line, then the body (the <title>/<body> of the reply).
  jq -r '
    def trim: gsub("^\\s+|\\s+$"; "");
    def tag($t): [match("<\($t)>([\\s\\S]*?)</\($t)>").captures[0].string][0] // "" | trim;
    if .is_error then
      "Claude Code: \(.result // .subtype)\(if .num_turns then ", after \(.num_turns) turns" else "" end)\(if .subtype == "error_max_turns" then " (raise CLAUDE_MAX_TURNS)" else "" end)\n" | halt_error(1)
    else . end
    | (.result // "" | trim) as $text | ($text | tag("title")) as $title
    | if $title != "" then [$title, ($text | tag("body"))]
      else ($text | split("\n")) as $l | [$l[0], ($l[1:] | join("\n") | trim)] end
    | "\(.[0] | sub("^#+\\s*"; "") | gsub("\\s+"; " ") | .[0:120])\n\n\(.[1])"
  ' "$BASE/tmp/result.json" > "$BASE/runs/$RUN.pr.md" || die "Claude Code failed: no PR"
  say "Claude done: $(jq -r '"\(.num_turns // "?") turns, \(((.duration_ms // 0) / 1000) | floor) s, $\((.total_cost_usd // 0) * 100 | round / 100)"' "$BASE/tmp/result.json")"
  say "Claude's proposal (becomes the pull request):"
  sed 's/^/  /' "$BASE/runs/$RUN.pr.md"
  echo
  if [ -z "$(git status --porcelain)" ]; then
    say "no change proposed, no sandbox, no PR"; run_done; exit 0
  fi
  if [ "$RUN_KIND" = task ]; then
    [ -s "$CHECK" ] || die "Claude wrote no feature check ($CHECK): no sandbox, no PR"
  fi
  printf 'STACK_DB_KIND=%s\nRUN_KIND=%s\n' "$STACK_DB_KIND" "$RUN_KIND" > "$STATE"
else
  # resume: the clone must still hold this run's change (a later run replaces it).
  [ -f "$STATE" ] && [ -f "$BASE/runs/$RUN.pr.md" ] || die "run $RUN has no change to resume (did Claude finish?)"
  [ "$(cat "$BASE/repo/.git/agent-run" 2>/dev/null)" = "$RUN" ] || die "the clone holds another run's change: run $RUN can't be resumed"
  RUN_KIND=migrate
  . "$STATE"
  cd "$BASE/repo"
  say "resuming run $RUN: what its sandbox left is deleted first"
  read_lists; delete_run "$RUN" "$computers" "$databases" "$secrets"
fi
echo "$RUN" > "$BASE/repo/.git/agent-run"

step "building the change (mvn package)"
mvn -q -B package >> "$BASE/log/build.log" 2>&1 ||
  { tail -40 "$BASE/log/build.log" >&2; die "build fails with the change: no PR"; }
# The jar the sandbox runs, as built here (no second build there).
APP_JAR=$(ls -t target/*.jar 2>/dev/null | grep -v -e '/original-' -e '-sources\.jar$' -e '-javadoc\.jar$' | head -1)
[ -n "$APP_JAR" ] || die "no jar in target/ after the build"

step "5/6 testing the change on sandbox $SANDBOX_COMPUTER"
nv checkpoint create -c "$BASE_COMPUTER" --name "$CHECKPOINT" --wait >/dev/null
nv restore "$CHECKPOINT" -c "$BASE_COMPUTER" --name "$SANDBOX_COMPUTER" --wait >/dev/null
# Right after a restore, DNS may not answer yet: wait for 5 lookups in a row to succeed.
host=$(json_field "$(status_json "$BASE_COMPUTER")" url | sed 's#^https://##; s#[:/].*##')
i=0 ok=0
while [ $ok -lt 5 ]; do
  if nevia exec -n "$SANDBOX_COMPUTER" -- getent hosts "$host" >/dev/null 2>&1; then ok=$((ok + 1)); else ok=0; fi
  i=$((i + 1)); [ $i -lt 120 ] || die "$SANDBOX_COMPUTER has no DNS after ~5 min (./agent.sh cleanup $RUN)"
  sleep 2
done
COMPUTER=$SANDBOX_COMPUTER SANDBOX=$RUN STACK_DB_KIND=$STACK_DB_KIND APP_JAR=$BASE/repo/$APP_JAR \
  logged "$BASE/log/validation.log" "$DEPLOY" deploy ||
  die "the change fails its checks on $SANDBOX_COMPUTER: no PR (./agent.sh resume $RUN once fixed, or cleanup)"
SANDBOX_URL=$(json_field "$(status_json "$SANDBOX_COMPUTER")" url)
if [ "$RUN_KIND" = task ]; then
  # Claude's script: run without the token, the Nevia key or Claude's credential (no more than
  # Claude itself could do), on the fork of the data the sandbox has.
  say "feature check on $SANDBOX_URL"
  key_off
  rc=0; logged "$BASE/log/feature.log" env -i PATH="$PATH" HOME="$WORK" sh "$CHECK" "$SANDBOX_URL" || rc=$?
  key_on
  [ $rc -eq 0 ] || die "the feature check fails on $SANDBOX_COMPUTER (exit $rc): no PR (./agent.sh resume $RUN, or cleanup)"
fi

step "6/6 opening the pull request"
PR=$BASE/tmp/pr.md
cp "$BASE/runs/$RUN.pr.md" "$PR"
TITLE=$(head -n 1 "$PR")
BRANCH=agent/$RUN
{
  if [ "$RUN_KIND" = migrate ]; then
    printf '\n## Data analysis (mongo2pg)\n\nGiven to Claude, read-only, without its sample values.\n\n'
    printf '| Collection | Documents | Max depth | Array fields | Score | Infer warnings |\n|---|---|---|---|---|---|\n'
    for f in "$ANALYSIS"/collections/*/*.stats.yaml; do
      [ -f "$f" ] || continue
      awk -v c="$(basename "$f" .stats.yaml)" '
        /^documents_in_collection:/ { d = $2 } /^depth_max:/ { p = $2 } /^array_field_count:/ { a = $2 }
        /^migrability_score:/ { s = $2 } /^ +field_path:/ { w = w (w ? ", " : "") "`" $2 "`" }
        END { printf "| `%s` | %s | %s | %s | %s | %s |\n", c, d, p, a, s, (w ? w : "none") }' "$f"
    done
  fi
  printf '\n## Automated validation\n\n- `%s` recreated from `%s` and checked before the change.\n' "$BASE_COMPUTER" "$BASE_BRANCH"
  if [ "$(app_db_kind)" = "$STACK_DB_KIND" ]; then db_note="a branch of the stack's database: a fork of its data"; else db_note="a new database, with the seed data"; fi
  printf -- '- `mvn package` passes with the change (the tests included).\n- Sandbox `%s` (checkpoint `%s` of `%s`, `database.kind`: `%s`, %s):\n\n```\n' \
    "$SANDBOX_COMPUTER" "$CHECKPOINT" "$BASE_COMPUTER" "$(app_db_kind)" "$db_note"
  sed 's/\x1b\[[0-9;]*m//g' "$BASE/log/validation.log" | grep -v '^\[app\]' | tail -8
  printf '```\n'
  if [ "$RUN_KIND" = task ]; then
    printf '\n### Feature check\n\nClaude'"'"'s script, run on the sandbox (a fork of the data):\n\n<details><summary>feature-check.sh</summary>\n\n```sh\n'
    cat "$CHECK"
    printf '```\n\n</details>\n\n```\n'
    sed 's/\x1b\[[0-9;]*m//g' "$BASE/log/feature.log" | tail -30
    printf '```\n'
  fi
  printf '\n## Preview\n\n%s — delete it once the PR is closed: `./agent.sh cleanup %s`\n' "$SANDBOX_URL" "$RUN"
  printf '\n---\nProposed by Claude Code running on a Nevia computer.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n'
} >> "$PR"
git checkout -q -B "$BRANCH"
git add -A
git diff --cached --quiet || git commit -q -m "$TITLE" -m "Proposed by Claude Code running on a Nevia computer (run $RUN)."
git_ push -q origin "$BRANCH"
payload=$(tail -n +2 "$PR" | jq -Rs --arg title "$TITLE" --arg head "$BRANCH" --arg base "$BASE_BRANCH" \
  '{title: $title, head: $head, base: $base, body: (. | gsub("^\\s+|\\s+$"; ""))}')
PR_URL=$(curl -fsS -X POST "https://api.github.com/repos/$REPO/pulls" \
        -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" -d "$payload" | jq -r .html_url) ||
  die "branch $BRANCH pushed but the pull request could not be created"
say "preview: $SANDBOX_URL"
say "pull request: $PR_URL"
run_done
