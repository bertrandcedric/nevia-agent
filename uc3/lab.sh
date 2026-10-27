#!/bin/sh
# nevia-helloworld's MongoDB database (hello: its customers) migrated to a Nevia Postgres by Claude
# Code with mongo2pg, on branches of the database, from a Nevia computer that holds the MongoDB.
# Config: ../.env only (UC3_ lines first, see ../.env.example).
#
# Usage: ./lab.sh setup               1. the computer, its Nevia key, the Postgres database, the
#                                        tools and MongoDB (empty)
#        ./lab.sh seed                2. customers generated into MongoDB (the collection replaced),
#                                        SEED_STRANGE percent of them strange
#        ./lab.sh analyse             3. mongo2pg: init, infer, export, CONTEXT.md; its reports in
#                                        results/analysis/
#        ./lab.sh migrate             4. clean slate (branches, database, project of earlier
#                                        runs), then Claude migrates, attempt after attempt, until
#                                        it is verified; the report in results/<run>/
#        ./lab.sh logs [<run>]        follow a run again (default: the latest), fetch its report
#        ./lab.sh stop                stop every run going (their branches stay)
#        ./lab.sh rotate-key          a new Nevia key for the computer, the old one revoked
#                                        (refused while a run is going)
#        ./lab.sh destroy [--results] [--yes]
#                                        delete uc3 on Nevia: its computer (MongoDB, its data, the
#                                        tools), its principal and key, the database and its
#                                        branches; --results: results/ too
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
TAG=lab
usage() { sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0"; }

CMD=${1:-}
case $CMD in
  setup|seed|analyse|migrate|logs|stop|rotate-key|destroy) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 1 ;;
esac
# The total time of the command, also when it fails.
case $CMD in logs) ;; *) trap 'rc=$?; done_ "./lab.sh $CMD"; exit $rc' EXIT ;; esac

# .env alone decides: the shell's values are dropped first.
unset NEVIA_WORKSPACE AGENT AGENT_KEY_TTL AGENT_KEY_SECRET COMPUTER LAB PG_NAME KEY_TTL SLEEP_AT_END ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN CLAUDE_MODEL CLAUDE_MAX_TURNS CLAUDE_BASH_MAX_OUTPUT \
  ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
  SEED_CUSTOMERS SEED SEED_STRANGE SEED_STRANGE_KINDS
load_env "$DIR/../.env" UC3
# uc3's own computer (COMPUTER, i.e. UC3_COMPUTER in .env; not the AGENT of uc1 and uc2), with
# its own principal and key (in the secret UC3_NEVIA_KEY), handled by agent/common/computer.sh:
# one key per computer, renewed by ./lab.sh rotate-key only, a key file per process, the runs
# registered, so that two uc3 commands may overlap.
AGENT=${COMPUTER:-uc3-agent} AGENT_KEY_SECRET=UC3_NEVIA_KEY
. "$DIR/../common/computer.sh"
LAB=$AGENT
PG_NAME=${PG_NAME:-uc3-m2p}
SLEEP_AT_END=${SLEEP_AT_END:-yes}
for c in nevia jq; do command -v $c >/dev/null || die "$c not found"; done
[ -n "${NEVIA_WORKSPACE:-}" ] || die "set NEVIA_WORKSPACE in .env"
export NEVIA_WORKSPACE
# The database, and the project and its schema (as remote/setup/env.sh names them).
MONGO_DB=hello NAME=hello

# wake: nevia exec never wakes a computer (exit 5); exit 6 (busy) is retried.
wake() {
  i=0
  while :; do
    rc=0; nv exec -n "$LAB" -- true >/dev/null 2>&1 || rc=$?
    case $rc in
      0) return ;;
      5) say "waking $LAB"; nv awake "$LAB" --wait >/dev/null; return ;;
      3) die "no computer $LAB: ./lab.sh setup" ;;
      6) i=$((i + 1)); [ $i -lt 12 ] || die "cannot reach $LAB"; sleep 5 ;;
      *) die "cannot reach $LAB (nevia exec exit $rc)" ;;
    esac
  done
}

# going_runs: the runs going on the computer, from the live processes (clean removes the files of
# earlier runs).
going_runs() {
  nv exec -n "$LAB" -- sh -c 'pgrep -af "uc3/scripts/migrate/run.sh [0-9]" |
    sed -n "s/.*run\.sh \([0-9]*\).*/\1/p" | sort -u | tr "\n" " "; true'
}

# push_scripts: lib.sh and remote/ (setup/, seed/, analyse/, migrate/) into ~/uc3/scripts
# (replaced), never under a run going: it reads them as it goes.
push_scripts() {
  going=$(going_runs)
  [ -z "${going% }" ] || die "run(s) ${going% } still going: ./lab.sh logs <run>, or ./lab.sh stop"
  stage=$(mktemp -d "${TMPDIR:-/tmp}/lab.XXXXXX")
  cp "$DIR/lib.sh" "$stage/" && cp -R "$DIR/remote/." "$stage/"
  set --
  # macOS tar would add xattrs and AppleDouble files.
  if tar --version 2>/dev/null | grep -q bsdtar; then set -- --no-xattrs --no-mac-metadata; fi
  COPYFILE_DISABLE=1 tar -czf - "$@" -C "$stage" . | base64 |
    nv exec -i "$LAB" -- sh -c 'd=~/uc3/scripts && rm -rf "$d" && mkdir -p "$d" && base64 -d | tar -xzf - -C "$d"'
  rm -rf "$stage"
}

# lab_env: the -e flags of every remote script, from .env.
lab_env() {
  set -- -e "LAB_NEVIA_WORKSPACE=$NEVIA_WORKSPACE" -e "PG_NAME=$PG_NAME"
  # Claude's gateway and its models (not its token: a secret, set by setup) go with each command.
  for v in CLAUDE_MODEL CLAUDE_MAX_TURNS CLAUDE_BASH_MAX_OUTPUT ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL SEED_CUSTOMERS SEED SEED_STRANGE SEED_STRANGE_KINDS; do
    eval "val=\${$v:-}"; [ -z "$val" ] || set -- "$@" -e "$v=$val"
  done
  printf '%s\n' "$@"
}

# remote <step>: the remote script of a step (setup, seed, analyse: remote/<step>/<step>.sh) on the
# computer, with the config from .env.
remote() {
  wake; push_scripts
  # One flag per line, no globbing.
  set -f; old_ifs=$IFS; IFS='
'
  set -- $(lab_env) "$LAB" -- sh -c 'exec sh ~/uc3/scripts/"$0"' "$1/$1.sh"
  IFS=$old_ifs; set +f
  nv exec -n "$@"
}

confirm() {
  [ "$2" = --yes ] && return
  [ -t 0 ] || die "$1 needs confirmation: $0 $1 --yes"
  printf '%s Type "%s": ' "$3" "$1"; read -r answer; [ "$answer" = "$1" ] || die "aborted"
}

# setup: uc3's computer, with its Nevia key (made only if there is none: agent/common/computer.sh)
# and Claude Code's credential, the Postgres database, then on the computer the tools and MongoDB.
setup() {
  agent_setup
  if [ -z "$(status_json pg "$PG_NAME")" ]; then
    say "creating Postgres database $PG_NAME"
    nv pg new "$PG_NAME" --wait >/dev/null
  fi
  say "ready: $LAB ($(json_field "$(status_json "$LAB")" url)), database $PG_NAME"
}

# follow <run>: print the run's new log lines until it writes its status; returns it.
follow() {
  n=0 fails=0
  while :; do
    rc=0
    out=$(nv exec -n "$LAB" -- sh -c '
          f=~/uc3/runs/$1.log
          [ -f "$f" ] || { echo "@@ 0 missing"; exit 0; }
          n=$(wc -l < "$f")
          [ "$n" -le "$2" ] || sed -n "$(($2 + 1)),${n}p" "$f"
          echo "@@ $n $(cat ~/uc3/runs/$1.status 2>/dev/null)"' sh "$1" "$n" 2>/dev/null) || rc=$?
    case $rc in
      0)
        fails=0
        printf '%s\n' "$out" | sed '$d'
        set -- "$1" $(printf '%s\n' "$out" | tail -n 1)
        n=$3
        case ${4:-} in '') ;; missing) die "no run $1 on $LAB" ;; *) return "$4" ;; esac ;;
      5) die "$LAB went to sleep: ./lab.sh logs $1" ;;
      *) fails=$((fails + 1)); [ $fails -lt 12 ] || die "lost contact with $LAB: ./lab.sh logs $1" ;;
    esac
    sleep 5
  done
}

# fetch_analysis: into results/analysis/ (replaced), what analyse made: the mongo2pg reports
# (reports/main.html), CONTEXT.md, the inferred schemas, stats and infer warnings, the mappings,
# the DDL and the documents of each collection. No CSV, no config: data and passwords.
fetch_analysis() {
  out=$DIR/results/analysis
  rm -rf "$out" && mkdir -p "$out"
  nv exec -n "$LAB" -- sh -c '
    cd ~/uc3/migration && tar -czf - --exclude=data --exclude=config --exclude=branches --exclude="*.csv*" \
      "$1" "$1.counts.json" 2>/dev/null | base64' sh "$NAME" |
    base64 -d | tar -xzf - -C "$out" || say "warning: could not fetch every file of the analysis"
  say "analysis: results/analysis/$NAME/reports/main.html, results/analysis/$NAME/context/CONTEXT.md"
}

# collect_usage <run>: the run's model, tokens and cost (runs/<run>.result.json, Claude Code's
# own count) added to results/usage.jsonl (one line per run), and results/usage.md made again
# from it: one row per run and the total. A run already collected is replaced.
collect_usage() {
  f=$DIR/results/$1/runs/$1.result.json all=$DIR/results/usage.jsonl
  [ -s "$f" ] || { say "no result.json in run $1: no usage"; return 0; }
  touch "$all"
  { jq -c --arg run "$1" 'select(.run != $run)' "$all"
    jq -c --arg run "$1" '{run: $run, model: (.modelUsage // {} | keys | join(", ")), turns: .num_turns,
      cost_usd: (.total_cost_usd // 0)} + (.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)})' "$f"; } > "$all.new" && mv "$all.new" "$all"
  jq -s -r '
    def usd: . * 100 | round / 100 | tostring | "$" + .;
    sort_by(.run) |
    "# Claude usage, all runs", "",
    "Tokens and cost as Claude Code counts them (list prices).", "",
    "| Run | Model | Turns | Input | Output | Cache write | Cache read | Cost |",
    "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    (.[] | "| \(.run) | \(.model) | \(.turns) | \(.input) | \(.output) | \(.cache_write) | \(.cache_read) | \(.cost_usd | usd) |"),
    "| **Total** | \(length) runs | \(map(.turns // 0) | add) | \(map(.input) | add) | \(map(.output) | add) | \(map(.cache_write) | add) | \(map(.cache_read) | add) | **\(map(.cost_usd) | add | usd)** |"
  ' "$all" > "$DIR/results/usage.md"
}

# fetch_results <run>: into results/<run>/, what the run produced: the report, Claude's work
# (its scripts and checks), the project (DDL, mappings, CONTEXT.md, the mongo2pg HTML
# reports), the log and Claude's summary. No CSV, no config, no node_modules: data and passwords.
fetch_results() {
  out=$DIR/results/$1
  mkdir -p "$out"
  nv exec -n "$LAB" -- sh -c '
    cd ~/uc3
    tar -czf - --exclude=node_modules --exclude=scratch --exclude="*.pristine" --exclude="*.csv*" \
      --exclude=config --exclude=data --exclude=branches \
      work "migration/$2" runs/$1.log runs/$1.result.json runs/$1.meta.json runs/$1.timing.txt \
      runs/$1.branches.txt runs/$1.task.txt runs/$1.cost.json 2>/dev/null | base64' sh "$1" "$NAME" |
    base64 -d | tar -xzf - -C "$out" || say "warning: could not fetch every result"
  rm -f "$out/INDEX.md" "$out/SUMMARY.md"
  collect_usage "$1"
  build_report "$1"
  say "report: results/$1/REPORT.md"
  r=$out/runs/$1.result.json
  # Each jq into a variable first: inside "...$(jq '...')...", macOS's sh (bash 3.2) misreads the
  # quotes of a program that has some, and brace-expands its {...}.
  if [ -s "$r" ]; then
    usage=$(jq -r '"\(.modelUsage // {} | keys | join(", ")), \(.num_turns) turns, tokens: \((.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)}) | "input \(.input), output \(.output), cache write \(.cache_write), cache read \(.cache_read)"); cost $\((.total_cost_usd // 0) * 100 | round / 100)"' "$r")
    say "usage: $usage"
  fi
  if [ -s "$DIR/results/usage.jsonl" ]; then
    all=$(jq -s -r '"\(length) runs, $\(map(.cost_usd) | add * 100 | round / 100)"' "$DIR/results/usage.jsonl")
    say "all runs: $all (results/usage.md)"
  fi
  # The verdict as the report's header has it (Claude's).
  say "verdict: $(sed -n 's/^| \*\*Verdict\*\* | \*\*\(.*\)\*\* |$/\1/p' "$out/REPORT.md")"
}

# cost_section <cost.json>: what the run cost (remote/migrate/cost.jq): in all, per document, per
# model (Claude Code's own count), per attempt (an estimate).
cost_section() {
  echo "## Cost"
  echo
  if [ ! -s "$1" ]; then echo "No cost.json: see Run details."; echo; return; fi
  jq -r '
    def usd: if . == null then "-" else . * 100 | round / 100 | tostring | "$" + . end;
    def usd4: if . == null then "-" else . * 10000 | round / 10000 | tostring | "$" + . end;
    "**\(.total | usd)** for \(.docs) documents, \(.per_doc | usd4) per document (Claude Code'"'"'s own count, list prices).", "",
    "| Model | Input | Output | Cache write | Cache read | Cost |", "| --- | ---: | ---: | ---: | ---: | ---: |",
    (.models[] | "| \(.model) | \(.input) | \(.output) | \(.cache_write) | \(.cache_read) | \(.cost | usd) |"),
    "",
    "Per attempt (≈: each model'"'"'s cost shared out by weighted tokens; the last attempt includes the main database):", "",
    "| Attempt | Messages | Cost |", "| --- | ---: | ---: |",
    (.attempts[] | "| \(.phase) | \(.messages // "-") | \(if .messages then "≈ " else "" end)\(.cost | usd) |"),
    ""' "$1"
}

# build_report <run>: results/<run>/REPORT.md, the one file to read. A header (verdict, source,
# target, attempts, durations, Claude), the cost, Claude's report (work/REPORT.md, its last line the
# verdict), the run's details and the files.
build_report() {
  out=$DIR/results/$1 meta=$DIR/results/$1/runs/$1.meta.json
  body=$out/work/REPORT.md
  m() { [ -s "$meta" ] && jq -r "$1 // \"?\"" "$meta" 2>/dev/null || echo "?"; }
  res=$out/runs/$1.result.json
  r() { [ -s "$res" ] && jq -r "$1" "$res" 2>/dev/null || echo "?"; }
  when() { case $1 in ''|*[!0-9]*) echo "?" ;; *) date -u -r "$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -d "@$1" '+%Y-%m-%d %H:%M UTC' ;; esac; }
  span() { case $1$2 in *[!0-9]*|'') echo "?" ;; *) dur $(($2 - $1)) ;; esac; }
  # The verdict: the last line of Claude's report.
  if [ -s "$body" ] && tail -n 1 "$body" | grep -q '^VERDICT: '; then
    verdict=$(tail -n 1 "$body" | sed 's/^VERDICT: //')
  else
    verdict="FAIL (no VERDICT line in Claude's report)"
  fi
  branches=$(awk 'NR > 1 && $2 ~ /-try[0-9]+$/ { sub(/.*-/, "", $2); printf "%s%s", sep, $2; sep = ", " }' "$out/runs/$1.branches.txt" 2>/dev/null)
  # A line of the timing breakdown (remote/timing.jq), by its label.
  timing() { sed -n "s/^ *$2/$2/p" "$out/runs/$1.timing.txt" 2>/dev/null | head -n 1; }
  {
    echo "# Migration report: MongoDB \`$(m .mongo_db)\` → Nevia Postgres \`$(m .pg_name)\`"
    echo
    echo "| | |"
    echo "| --- | --- |"
    echo "| **Verdict** | **$verdict** |"
    vj=$out/work/verify-main.json
    [ ! -s "$vj" ] || echo "| verify.mjs on the main database | $(jq -r '"\(.pass) PASS, \(.fail) FAIL (\(.at // "?"))"' "$vj" 2>/dev/null || echo "?") |"
    echo "| Run | $1, $(when "$(m .started)") |"
    echo "| Source | MongoDB on the computer, database \`$(m .mongo_db)\` (nevia-helloworld's customers, lab.sh seed) |"
    echo "| Target | Nevia PostgreSQL \`$(m .pg_name)\`, schema \`$(m .schema)\` |"
    echo "| Attempts | ${branches:-none} (each kept as a branch \`$(m .pg_name)-tryN\`) |"
    echo "| Duration | $(span "$(m .started)" "$(m .ended)") in all, Claude $(span "$(m .claude_started)" "$(m .ended)"): $(timing "$1" model), $(timing "$1" commands) |"
    echo "| Claude | $(m .model), $(m .turns) turns, \$$(m '(.cost_usd * 100 | round / 100)') |"
    echo "| Tokens | $(r '(.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)}) | "input \(.input), output \(.output), cache write \(.cache_write), cache read \(.cache_read)"') |"
    echo
    cost_section "$out/runs/$1.cost.json"
    echo "## Claude's report"
    echo
    # Its headings one level down, under this one.
    if [ -s "$body" ]; then grep -v '^VERDICT:' "$body" | sed 's/^#/##/'; else echo "No report: see the log, runs/$1.log."; fi
    echo
    echo "## Run details"
    echo
    echo "### Timing"
    echo
    printf '```text\n%s\n```\n\n' "$(cat "$out/runs/$1.timing.txt" 2>/dev/null)"
    echo "### Databases at the end"
    echo
    printf '```text\n%s\n```\n\n' "$(cat "$out/runs/$1.branches.txt" 2>/dev/null)"
    if [ -s "$res" ]; then
      echo "### Tokens and cost"
      echo
      echo "Claude Code's own count, at list prices. All runs: [../usage.md](../usage.md)."
      echo
      jq -r '
        "| Model | Input | Output | Cache write | Cache read | Cost |", "| --- | ---: | ---: | ---: | ---: | ---: |",
        (.modelUsage // {} | to_entries[] | .value as $u |
          "| \(.key) | \($u.inputTokens // 0) | \($u.outputTokens // 0) | \($u.cacheCreationInputTokens // 0) | \($u.cacheReadInputTokens // 0) | $\(($u.costUSD // 0) * 100 | round / 100) |"),
        ((.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)}) as $t | "| **Total** | \($t.input) | \($t.output) | \($t.cache_write) | \($t.cache_read) | **$\((.total_cost_usd // 0) * 100 | round / 100)** |")' "$res"
      echo
    fi
    echo "### Versions"
    echo
    echo "- Claude Code: $(m .versions.claude)"
    echo "- $(m .versions.mongo2pg)"
    echo "- $(m .versions.psql)"
    echo
    if [ -s "$out/runs/$1.result.json" ]; then
      echo "### Claude's final answer"
      echo
      jq -r '.result // .subtype' "$out/runs/$1.result.json" | sed 's/^/> /'
      echo
    fi
    echo "## Files"
    echo
    for f in work/REPORT.md "migration/$NAME/context/CONTEXT.md" \
             "migration/$NAME/reports/main.html" "migration/$NAME/reports/post_report.html" runs/$1.task.txt runs/$1.log; do
      [ ! -e "$out/$f" ] || echo "- [$f]($f)"
    done
    echo "- work/: $(cd "$out/work" 2>/dev/null && ls | tr '\n' ' ')"
    echo "- migration/$NAME/: the mongo2pg project as the run left it (DDL in schema/, mappings in source/), without its CSV and config"
  } > "$out/REPORT.md"
}

# at_end: after a run, the computer to sleep unless SLEEP_AT_END=no; never under a run going,
# of any use case (agent_sleep).
at_end() {
  [ "$SLEEP_AT_END" != no ] || return 0
  agent_sleep
}

migrate() {
  wake; push_scripts
  RUN=$(date -u +%y%m%d%H%M%S)
  say "run $RUN: MongoDB $MONGO_DB to $PG_NAME, on $LAB"
  # A run outlasts a nevia exec: started detached, it writes <run>.log, then <run>.status.
  # One flag per line, no globbing (a URI holds ?).
  set -f; old_ifs=$IFS; IFS='
'
  set -- $(lab_env) "$LAB" -- sh -c '
    mkdir -p ~/uc3/runs && cd ~/uc3/runs && setsid sh -c "sh ~/uc3/scripts/migrate/run.sh \"\$1\" > \$1.log 2>&1; echo \$? > \$1.status" \
      sh "$1" < /dev/null > /dev/null 2>&1 &' sh "$RUN"
  IFS=$old_ifs; set +f
  nv exec -n "$@"
  say "started (Ctrl-C stops following, not the run: ./lab.sh logs $RUN)"
  rc=0; follow "$RUN" || rc=$?
  fetch_results "$RUN"
  at_end
  [ $rc -eq 0 ] || die "run $RUN failed (exit $rc): its log above, results/$RUN/"
  say "migrated and verified: results/$RUN/REPORT.md"
}

logs() {
  wake
  set -- "${1:-$(nv exec -n "$LAB" -- sh -c 'ls -t ~/uc3/runs/*[0-9].log 2>/dev/null | head -n 1 | xargs -r basename -s .log')}"
  case $1 in ''|*[!0-9]*) die "which run? the ids are in results/" ;; esac
  say "run $1"
  rc=0; follow "$1" || rc=$?
  fetch_results "$1"
  at_end
  [ $rc -eq 0 ] || die "run $1 failed (exit $rc)"
}

# stop: every run going, Claude included: all the processes of its session (started by setsid),
# found from the live processes (not from files, which clean may have removed), marked 130.
stop() {
  wake
  nv exec -n "$LAB" -- sh -c '
    runs=$(pgrep -af "uc3/scripts/migrate/run.sh [0-9]" | sed -n "s/.*run\.sh \([0-9]*\).*/\1/p" | sort -u)
    [ -n "$runs" ] || { echo "[lab] no run going"; exit 0; }
    for run in $runs; do
      for sid in $(ps -o sid= -p "$(pgrep -d, -f "migrate/run.sh $run")" 2>/dev/null | sort -u); do
        pkill -TERM -s "$sid"
      done
    done
    sleep 5
    for run in $runs; do
      for sid in $(ps -o sid= -p "$(pgrep -d, -f "migrate/run.sh $run")" 2>/dev/null | sort -u); do
        pkill -KILL -s "$sid"
      done
      [ -e ~/uc3/runs/$run.status ] || echo 130 > ~/uc3/runs/$run.status
      echo "[lab] stopped run $run"
    done'
}

# destroy [--results] [--yes]: what uc3 has on Nevia, listed, then deleted after a confirmation:
# its computer (MongoDB, its data, the tools, the runs), its principal and key, the database and
# its branches; with --results, results/ here too. Claude Code's credential (a secret uc1 and uc2
# use too) stays: ../computer.sh destroy deletes it.
destroy() {
  results=no yes=
  for a in "$@"; do
    case $a in --results) results=yes ;; --yes) yes=--yes ;; *) die "destroy: unknown option $a" ;; esac
  done
  computer=$(status_json "$LAB")
  [ -z "$computer" ] || { busy=$(agent_busy); [ -z "$busy" ] || die "runs going on $LAB ($busy): ./lab.sh stop first"; }
  dbs=$(nv --json pg ls | jq -r --arg p "$PG_NAME" '[.. | objects | select(has("name")) | .name | select(. == $p or startswith($p + "-"))] | unique | sort_by(length)[]')
  principal=$(agent_principal)
  key=; ! agent_has_secret "$AGENT_KEY_SECRET" || key=$AGENT_KEY_SECRET
  [ -n "$computer$dbs$principal$key" ] || [ "$results" = yes ] || { say "nothing to delete in workspace $NEVIA_WORKSPACE"; return 0; }
  echo "In Nevia workspace $NEVIA_WORKSPACE, this deletes:"
  [ -z "$computer" ] || echo "  - computer $LAB ($(json_field "$computer" url)): MongoDB and its data, the tools, the runs"
  for d in $dbs; do echo "  - database $d"; done
  [ -z "$key" ] || echo "  - secret $key"
  [ -z "$principal" ] || echo "  - Nevia principal $LAB and its key"
  echo "Kept: Claude Code's credential (shared with uc1 and uc2: ../computer.sh destroy)"
  if [ "$results" = yes ]; then echo "On this machine: results/ (every run's report, the usage)"
  else echo "Kept: results/ on this machine (destroy --results deletes it too)"; fi
  confirm destroy "$yes" "This cannot be undone."
  [ -z "$computer" ] || { say "deleting computer $LAB"; nv --yes rm "$LAB" --wait >/dev/null; }
  # The branches first, then their parent.
  for d in $(printf '%s\n' $dbs | sort -r); do say "deleting database $d"; nv --yes pg rm "$d" --wait >/dev/null; done
  [ -z "$key" ] || { say "deleting secret $key"; nv --yes secrets unset "$key" >/dev/null; }
  [ -z "$principal" ] || { say "deleting Nevia principal $LAB"; nv --yes access rm "$principal" >/dev/null; }
  [ "$results" = no ] || { say "deleting results/"; rm -rf "$DIR/results"; }
  say "deleted; ./lab.sh setup starts again"
}

case $CMD in
  setup)   setup; remote setup ;;
  seed)    remote seed ;;
  analyse) remote analyse; fetch_analysis ;;
  migrate) migrate ;;
  logs)    logs "${2:-}" ;;
  stop)    stop ;;
  rotate-key) agent_rotate_key ;;
  destroy) shift; destroy "$@" ;;
esac
