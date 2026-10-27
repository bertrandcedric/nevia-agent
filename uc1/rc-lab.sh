#!/bin/sh
# A MongoDB database migrated to a Nevia Postgres by Claude Code with mongo2pg, on branches of the
# database, from a Nevia computer. The source is any MongoDB the computer reaches (MONGO_URI,
# MONGO_DB), or one that sources/<SOURCE>/ sets up there. Config: ../.env only (UC1_ lines
# first, see ../.env.example).
#
# Usage: ./rc-lab.sh all                 the 4 steps: setup, seed (with a SOURCE), analyse, migrate
#        ./rc-lab.sh setup               1. the computer, its Nevia key, the Postgres database
#        ./rc-lab.sh seed                2. the source on the computer, then its data (SOURCE)
#        ./rc-lab.sh analyse             3. mongo2pg: init, infer, export, CONTEXT.md
#        ./rc-lab.sh migrate             4. clean slate (branches, database, project of earlier
#                                        runs), then Claude migrates until it is verified
#        ./rc-lab.sh up                  the source alone, started again (SOURCE)
#        ./rc-lab.sh logs [<run>]        follow a run (default: the latest)
#        ./rc-lab.sh stop                stop every run going (their branches stay)
#        ./rc-lab.sh status              computer, containers, database and branches
#        ./rc-lab.sh sleep               put the computer to sleep, unless a run of any use case is
#                                        going (migrate does it at its end)
#        ./rc-lab.sh destroy [--results] [--yes]
#                                        delete what is uc1's on Nevia: on the computer ~/m2p and
#                                        the source (Rocket.Chat, its MongoDB and data), the
#                                        database and its branches; --results: results/ too. The
#                                        computer is shared with uc2 and uc3: ../computer.sh
#                                        destroy deletes it
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
TAG=rc-lab
usage() { sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0"; }

CMD=${1:-}
case $CMD in
  all|setup|up|seed|analyse|migrate|logs|stop|status|sleep|destroy) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 1 ;;
esac
# The total time of the command, also when it fails.
case $CMD in status|logs) ;; *) trap 'rc=$?; done_ "./rc-lab.sh $CMD"; exit $rc' EXIT ;; esac

# .env alone decides: the shell's values are dropped first.
unset NEVIA_WORKSPACE AGENT AGENT_KEY_TTL LAB PG_NAME KEY_TTL SLEEP_AT_END ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN CLAUDE_MODEL CLAUDE_MAX_TURNS CLAUDE_BASH_MAX_OUTPUT \
  ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
  SOURCE MONGO_URI MONGO_DB LAB_HTTPS_PORT SEED_USERS SEED_CHANNELS SEED_GROUPS SEED_DMS SEED_MESSAGES SEED
load_env "$DIR/../.env" UC1
# The source: rocketchat unless .env says otherwise (empty: an existing MongoDB, nothing to set up).
SOURCE=${SOURCE-rocketchat}
if [ -n "$SOURCE" ]; then
  [ -f "$DIR/sources/$SOURCE/source.sh" ] || die "no sources/$SOURCE/source.sh: SOURCE in .env"
  load_env "$DIR/sources/$SOURCE/source.env"
fi
# The computer, its Nevia key and the runs going on it: shared with uc2 and uc3 (AGENT).
. "$DIR/../common/computer.sh"
LAB=$AGENT
PG_NAME=${PG_NAME:-rc-pg}
SLEEP_AT_END=${SLEEP_AT_END:-yes}
for c in nevia jq; do command -v $c >/dev/null || die "$c not found"; done
[ -n "${NEVIA_WORKSPACE:-}" ] || die "set NEVIA_WORKSPACE in .env"
[ -n "${MONGO_URI:-}" ] && [ -n "${MONGO_DB:-}" ] || die "set MONGO_URI and MONGO_DB in .env (or a SOURCE)"
export NEVIA_WORKSPACE
# The project and its schema: the database's name as an identifier (as remote/setup/env.sh makes it).
NAME=$(slug "$MONGO_DB")

# wake: nevia exec never wakes a computer (exit 5); exit 6 (busy) is retried.
wake() {
  i=0
  while :; do
    rc=0; nv exec -n "$LAB" -- true >/dev/null 2>&1 || rc=$?
    case $rc in
      0) return ;;
      5) say "waking $LAB"; nv awake "$LAB" --wait >/dev/null; return ;;
      3) die "no computer $LAB: ./rc-lab.sh setup" ;;
      6) i=$((i + 1)); [ $i -lt 12 ] || die "cannot reach $LAB"; sleep 5 ;;
      *) die "cannot reach $LAB (nevia exec exit $rc)" ;;
    esac
  done
}

# going_runs: the runs going on the computer, from the live processes (clean removes the files of
# earlier runs).
going_runs() {
  nv exec -n "$LAB" -- sh -c 'pgrep -af "m2p/scripts/migrate/run.sh [0-9]" |
    sed -n "s/.*run\.sh \([0-9]*\).*/\1/p" | sort -u | tr "\n" " "; true'
}

# push_scripts: lib.sh, remote/ (setup/, analyse/, migrate/) and the source's sources/<SOURCE>/
# (as source/) into ~/m2p/scripts (replaced), never under a run going: it reads them as it goes.
push_scripts() {
  going=$(going_runs)
  [ -z "${going% }" ] || die "run(s) ${going% } still going: ./rc-lab.sh logs <run>, or ./rc-lab.sh stop"
  stage=$(mktemp -d "${TMPDIR:-/tmp}/rc-lab.XXXXXX")
  cp "$DIR/lib.sh" "$stage/" && cp -R "$DIR/remote/." "$stage/"
  [ -z "$SOURCE" ] || cp -R "$DIR/sources/$SOURCE" "$stage/source"
  set --
  # macOS tar would add xattrs and AppleDouble files.
  if tar --version 2>/dev/null | grep -q bsdtar; then set -- --no-xattrs --no-mac-metadata; fi
  COPYFILE_DISABLE=1 tar -czf - "$@" -C "$stage" . | base64 |
    nv exec "$LAB" -- sh -c 'd=~/m2p/scripts && rm -rf "$d" && mkdir -p "$d" && base64 -d | tar -xzf - -C "$d"'
  rm -rf "$stage"
}

# lab_env: the -e flags of every remote script, from .env (and the source's defaults).
lab_env() {
  set -- -e "LAB_NEVIA_WORKSPACE=$NEVIA_WORKSPACE" -e "PG_NAME=$PG_NAME" -e "LAB_MONGO_URI=$MONGO_URI" -e "MONGO_DB=$MONGO_DB"
  # Claude's gateway and its models (not its token: a secret, set by setup) go with each command.
  for v in CLAUDE_MODEL CLAUDE_MAX_TURNS CLAUDE_BASH_MAX_OUTPUT ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL SEED_USERS SEED_CHANNELS SEED_GROUPS SEED_DMS SEED_MESSAGES SEED; do
    eval "val=\${$v:-}"; [ -z "$val" ] || set -- "$@" -e "$v=$val"
  done
  printf '%s\n' "$@"
}

# lab <command> [args]: the remote script of a command on the computer, with the config from
# .env: analyse (remote/analyse/analyse.sh), status (remote/setup/status.sh), source <cmd> (the
# source's source.sh <cmd>, e.g. up, seed).
lab() {
  case $1 in
    analyse) script=analyse/analyse.sh ;;
    status) script=setup/status.sh ;;
    source) [ -n "$SOURCE" ] || die "no source scripts: SOURCE is empty in .env (an existing MongoDB)"
            script=source/source.sh ;;
    *) die "lab: unknown command '$1'" ;;
  esac
  shift
  url=$(json_field "$(status_json "$LAB")" url)
  # One flag per line, no globbing (a URI holds ?).
  set -f; old_ifs=$IFS; IFS='
'
  set -- $(lab_env) -e "LAB_URL=${url:-http://localhost}" "$LAB" -- sh -c 'exec sh ~/m2p/scripts/"$0" "$@"' "$script" "$@"
  IFS=$old_ifs; set +f
  nv exec -n "$@"
}
remote() { wake; push_scripts; lab "$@"; }

confirm() {
  [ "$2" = --yes ] && return
  [ -t 0 ] || die "$1 needs confirmation: $0 $1 --yes"
  printf '%s Type "%s": ' "$3" "$1"; read -r answer; [ "$answer" = "$1" ] || die "aborted"
}

# setup: the computer shared with uc2 and uc3, with its Nevia key (made only if there is none:
# agent/common/computer.sh) and the source's port (LAB_HTTPS_PORT, if any) published on 443, then
# the Postgres database.
setup() {
  set --
  [ -z "${LAB_HTTPS_PORT:-}" ] || { say "port $LAB_HTTPS_PORT of $LAB published on 443"; set -- -p "443:$LAB_HTTPS_PORT"; }
  agent_setup "$@"
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
          f=~/m2p/runs/$1.log
          [ -f "$f" ] || { echo "@@ 0 missing"; exit 0; }
          n=$(wc -l < "$f")
          [ "$n" -le "$2" ] || sed -n "$(($2 + 1)),${n}p" "$f"
          echo "@@ $n $(cat ~/m2p/runs/$1.status 2>/dev/null)"' sh "$1" "$n" 2>/dev/null) || rc=$?
    case $rc in
      0)
        fails=0
        printf '%s\n' "$out" | sed '$d'
        set -- "$1" $(printf '%s\n' "$out" | tail -n 1)
        n=$3
        case ${4:-} in '') ;; missing) die "no run $1 on $LAB" ;; *) return "$4" ;; esac ;;
      5) die "$LAB went to sleep: ./rc-lab.sh logs $1" ;;
      *) fails=$((fails + 1)); [ $fails -lt 12 ] || die "lost contact with $LAB: ./rc-lab.sh logs $1" ;;
    esac
    sleep 5
  done
}

# collect_issues <run>: the mongo2pg issues of the run (work/mongo2pg-issues.json, written by
# Claude) added to results/mongo2pg-issues.jsonl (one line per run and issue), and
# results/mongo2pg-issues.md made again from it: each issue, in how many runs, its last details.
# For improving mongo2pg; a run already collected is replaced.
collect_issues() {
  f=$DIR/results/$1/work/mongo2pg-issues.json all=$DIR/results/mongo2pg-issues.jsonl
  [ -s "$f" ] || { say "no mongo2pg-issues.json in run $1"; return 0; }
  jq -e 'type == "array"' "$f" >/dev/null 2>&1 || { say "warning: $f is not a JSON array"; return 0; }
  touch "$all"
  { jq -c --arg run "$1" 'select(.run != $run)' "$all"; jq -c --arg run "$1" '.[] | {run: $run} + .' "$f"; } > "$all.new" &&
    mv "$all.new" "$all"
  jq -s -r '
    def cell: tostring | gsub("\\|"; "\\|") | gsub("\n"; " ");
    group_by(.id) | sort_by(-length, .[0].id) |
    "# mongo2pg issues, all runs", "",
    "From each run'"'"'s work/mongo2pg-issues.json. Runs: \(map(.[].run) | unique | length). Known: in remote/migrate/known-issues.md, fixed before the first attempt; last seen: the last run where it still showed.", "",
    "| Issue | Known | Kind | Command | Runs | Last seen | Places (last run) | Observed | Expected | Suggestion |",
    "| --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- |",
    (.[] | (map(.run) | unique) as $runs | (sort_by(.run) | last) as $l |
      "| \($l.id | cell) | \(if $l.known == true then "yes" else "no" end) | \($l.kind | cell) | \($l.command | cell) | \($runs | length) | \($l.run) | \($l.places | join(", ") | cell) | \($l.observed | cell) | \($l.expected | cell) | \($l.suggestion | cell) |")
  ' "$all" > "$DIR/results/mongo2pg-issues.md"
  say "mongo2pg issues: $(jq length "$f") in run $1, results/mongo2pg-issues.md for all runs"
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
# (verify.mjs, its SQL and scripts), the project (DDL, mappings, CONTEXT.md, the mongo2pg HTML
# reports), the log and Claude's summary. No CSV, no config, no node_modules: data and passwords.
fetch_results() {
  out=$DIR/results/$1
  mkdir -p "$out"
  nv exec -n "$LAB" -- sh -c '
    cd ~/m2p
    tar -czf - --exclude=node_modules --exclude=scratch --exclude="*.pristine" --exclude="*.csv*" \
      --exclude=config --exclude=data --exclude=branches \
      work "migration/$2" runs/$1.log runs/$1.result.json runs/$1.meta.json runs/$1.timing.txt \
      runs/$1.branches.txt runs/$1.task.txt 2>/dev/null | base64' sh "$1" "$NAME" |
    base64 -d | tar -xzf - -C "$out" || say "warning: could not fetch every result"
  rm -f "$out/INDEX.md" "$out/SUMMARY.md"
  collect_issues "$1"
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
  # The verdict as the report's header has it (verify-main.json's).
  say "verdict: $(sed -n 's/^| \*\*Verdict\*\* | \*\*\(.*\)\*\* |$/\1/p' "$out/REPORT.md")"
}

# collections_section <verify.json>: section 2 of the report, from verify.mjs's results on the
# main database: each collection, its documents and rows, and the empty collections skipped.
collections_section() {
  echo "## 2. Collections"
  echo
  if [ ! -s "$1" ]; then echo "No verify-main.json: verify.mjs did not run on the main database."; echo; return; fi
  jq -r '
    "| Collection | Documents in MongoDB | Root table | Rows in Postgres | Child tables |",
    "| --- | ---: | --- | ---: | ---: |",
    (.collections[] | "| \(.collection) | \(.mongo) | \(.root) | \(.pg) | \(.child_tables) |"),
    "",
    "Skipped as empty (excluded in the config), \(.excluded | length): \(.excluded | map("`" + . + "`") | join(", ") | if . == "" then "none" else . end).",
    ""' "$1"
}

# verification_section <verify.json> <verify.txt>: section 6, verify.mjs's results on the main
# database: the counts per group of checks, the failures, and every check (folded).
verification_section() {
  echo "## 6. Verification on the main database"
  echo
  if [ ! -s "$1" ]; then echo "No verify-main.json: verify.mjs did not run on the main database."; echo; return; fi
  jq -r '
    "**\(.pass) PASS, \(.fail) FAIL** (\(.at)).", "",
    "| Checks | PASS | FAIL |", "| --- | ---: | ---: |",
    (.groups[] | "| \(.group) | \(.pass) | \(.fail) |"),
    "",
    (if .fail > 0 then "Failures:", "", (.results[] | select(.ok | not) | "- [\(.group)] \(.name): \(.detail // "")"), "" else empty end)' "$1"
  if [ -s "$2" ]; then
    printf '<details><summary>Every check (verify-main.txt)</summary>\n\n```text\n%s\n```\n\n</details>\n\n' "$(cat "$2")"
  fi
}

# build_report <run>: results/<run>/REPORT.md, the one file to read. A header (verdict, source,
# target, attempts, durations, Claude), Claude's report (work/REPORT.md, sections 1, 3, 4, 5) with
# the collections (2) and the verification on the main database (6) from work/verify-main.json,
# the mongo2pg issues (from work/mongo2pg-issues.json), the run's details and the files.
build_report() {
  out=$DIR/results/$1 meta=$DIR/results/$1/runs/$1.meta.json
  body=$out/work/REPORT.md issues=$out/work/mongo2pg-issues.json
  vjson=$out/work/verify-main.json vtxt=$out/work/verify-main.txt
  m() { [ -s "$meta" ] && jq -r "$1 // \"?\"" "$meta" 2>/dev/null || echo "?"; }
  res=$out/runs/$1.result.json
  r() { [ -s "$res" ] && jq -r "$1" "$res" 2>/dev/null || echo "?"; }
  when() { case $1 in ''|*[!0-9]*) echo "?" ;; *) date -u -r "$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -d "@$1" '+%Y-%m-%d %H:%M UTC' ;; esac; }
  span() { case $1$2 in *[!0-9]*|'') echo "?" ;; *) dur $(($2 - $1)) ;; esac; }
  # The verdict: verify.mjs's on the main database (older runs: the last line of Claude's report).
  if [ -s "$vjson" ]; then
    verdict=$(jq -r 'if .fail == 0 and .pass > 0 then "PASS" else "FAIL (\(.fail) of \(.pass + .fail) checks on the main database)" end' "$vjson" 2>/dev/null || echo "?")
  elif [ -s "$body" ] && tail -n 1 "$body" | grep -q '^VERDICT: '; then
    verdict=$(tail -n 1 "$body" | sed 's/^VERDICT: //')
  else
    verdict="FAIL (verify.mjs did not run on the main database)"
  fi
  branches=$(awk 'NR > 1 && $2 ~ /-try[0-9]+$/ { sub(/.*-/, "", $2); printf "%s%s", sep, $2; sep = ", " }' "$out/runs/$1.branches.txt" 2>/dev/null)
  nissues=0
  if [ -s "$issues" ]; then nissues=$(jq -e 'if type == "array" then length else error end' "$issues" 2>/dev/null) || nissues="? (mongo2pg-issues.json is not a JSON array)"; fi
  # A line of the timing breakdown (remote/timing.jq), by its label.
  timing() { sed -n "s/^ *$2/$2/p" "$out/runs/$1.timing.txt" 2>/dev/null | head -n 1; }
  {
    echo "# Migration report: MongoDB \`$(m .mongo_db)\` → Nevia Postgres \`$(m .pg_name)\`"
    echo
    echo "| | |"
    echo "| --- | --- |"
    echo "| **Verdict** | **$verdict** |"
    echo "| Run | $1, $(when "$(m .started)") |"
    echo "| Source | MongoDB, database \`$(m .mongo_db)\`${SOURCE:+ (set up by sources/$SOURCE)} |"
    echo "| Target | Nevia PostgreSQL \`$(m .pg_name)\`, schema \`$(m .schema)\` |"
    echo "| Attempts | ${branches:-none} (each kept as a branch \`$(m .pg_name)-tryN\`) |"
    echo "| Duration | $(span "$(m .started)" "$(m .ended)") in all, Claude $(span "$(m .claude_started)" "$(m .ended)"): $(timing "$1" model), $(timing "$1" commands) |"
    echo "| Claude | $(m .model), $(m .turns) turns, \$$(m '(.cost_usd * 100 | round / 100)') |"
    echo "| Tokens | $(r '(.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)}) | "input \(.input), output \(.output), cache write \(.cache_write), cache read \(.cache_read)"') (section 8) |"
    echo "| mongo2pg issues | $nissues (section 7) |"
    echo
    if [ -s "$body" ]; then
      # Section 2 before Claude's section 3 (at the end if it has none), section 6 after its 5.
      grep -v '^VERDICT:' "$body" | SECT2=$(collections_section "$vjson") awk '
        /^## 3\./ && !done { print ENVIRON["SECT2"]; print ""; done = 1 }
        { print }
        END { if (!done) { print ""; print ENVIRON["SECT2"] } }'
      echo
    else
      echo "## 1. Result"; echo; echo "No report: see the log, runs/$1.log."; echo
      collections_section "$vjson"
    fi
    verification_section "$vjson" "$vtxt"
    echo "## 7. mongo2pg issues"
    echo
    echo "Every change made to what mongo2pg generated, to improve the tool. All runs: [../mongo2pg-issues.md](../mongo2pg-issues.md)."
    echo
    if [ "${nissues%% *}" != "?" ] && [ "$nissues" -gt 0 ]; then
      jq -r '
        def v: if . == null or . == "" then "-" elif type == "array" then join(", ") else tostring end;
        def cell: v | gsub("\\|"; "\\|") | gsub("\n"; " ");
        "New issues first; known ones (remote/migrate/known-issues.md) still in this mongo2pg after them.", "",
        "| Issue | New | Kind | Command | Places | First seen |", "| --- | --- | --- | --- | ---: | --- |",
        (sort_by(.known == true) | .[] | "| [\(.id | cell)](#\(.id | v | ascii_downcase | gsub("[^a-z0-9 -]"; "") | gsub(" "; "-"))) | \(if .known == true then "known" else "**new**" end) | \(.kind | cell) | \(.command | cell) | \(.places | length) | \(.first_seen | cell) |"),
        "",
        (sort_by(.known == true) | .[] | "### \(.id | v)", "",
          "- **New or known:** \(if .known == true then "known (remote/migrate/known-issues.md)" else "new" end)",
          "- **Kind, command:** \(.kind | v), `\(.command | v)`",
          "- **Places:** \(.places | v)",
          "- **First seen:** \(.first_seen | v)",
          "- **Observed:** \(.observed | v)",
          "- **Expected:** \(.expected | v)",
          "- **Workaround:** \(.workaround | v)",
          "- **Suggestion for mongo2pg:** \(.suggestion | v)", "")' "$issues"
    else
      echo "None recorded."
      echo
    fi
    echo "## 8. Run details"
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
    for f in work/REPORT.md work/mongo2pg-issues.json work/verify-main.txt work/verify.mjs work/known-fixes.sql \
             work/known-issues.md "migration/$NAME/context/CONTEXT.md" \
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
    mkdir -p ~/m2p/runs && cd ~/m2p/runs && setsid sh -c "sh ~/m2p/scripts/migrate/run.sh \"\$1\" > \$1.log 2>&1; echo \$? > \$1.status" \
      sh "$1" < /dev/null > /dev/null 2>&1 &' sh "$RUN"
  IFS=$old_ifs; set +f
  nv exec -n "$@"
  say "started (Ctrl-C stops following, not the run: ./rc-lab.sh logs $RUN)"
  rc=0; follow "$RUN" || rc=$?
  fetch_results "$RUN"
  at_end
  [ $rc -eq 0 ] || die "run $RUN failed (exit $rc): its log above, results/$RUN/"
  say "migrated and verified: results/$RUN/REPORT.md"
}

logs() {
  wake
  set -- "${1:-$(nv exec -n "$LAB" -- sh -c 'ls -t ~/m2p/runs/*[0-9].log 2>/dev/null | head -n 1 | xargs -r basename -s .log')}"
  case $1 in ''|*[!0-9]*) die "which run? ./rc-lab.sh status" ;; esac
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
    runs=$(pgrep -af "m2p/scripts/migrate/run.sh [0-9]" | sed -n "s/.*run\.sh \([0-9]*\).*/\1/p" | sort -u)
    [ -n "$runs" ] || { echo "[rc-lab] no run going"; exit 0; }
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
      [ -e ~/m2p/runs/$run.status ] || echo 130 > ~/m2p/runs/$run.status
      echo "[rc-lab] stopped run $run"
    done'
}

status() {
  nv status "$LAB" || true
  if nv exec -n "$LAB" -- true >/dev/null 2>&1; then
    # During a run, its scripts as they are.
    going=$(going_runs); [ -n "${going% }" ] && say "run(s) going: ${going% }" || push_scripts
    lab status
  else
    say "$LAB is asleep"
  fi
  nv pg ls | awk -v p="$PG_NAME" 'NR == 1 || $2 == p || index($2, p "-") == 1'
}

# destroy [--results] [--yes]: what is uc1's, listed, then deleted after a confirmation: on the
# computer ~/m2p and the source (Rocket.Chat and its MongoDB: containers, volume, ~/rocketchat),
# the database and its branches; with --results, results/ here too. The computer, its key and
# Claude Code's secrets are shared with uc2 and uc3: ../computer.sh destroy deletes them.
destroy() {
  results=no yes=
  for a in "$@"; do
    case $a in --results) results=yes ;; --yes) yes=--yes ;; *) die "destroy: unknown option $a" ;; esac
  done
  computer=$(status_json "$LAB")
  if [ -n "$computer" ]; then
    wake
    going=$(going_runs); [ -z "${going% }" ] || die "run(s) ${going% } going: ./rc-lab.sh stop first"
  fi
  dbs=$(nv --json pg ls | jq -r --arg p "$PG_NAME" '[.. | objects | select(has("name")) | .name | select(. == $p or startswith($p + "-"))] | unique | sort_by(length)[]')
  [ -n "$computer$dbs" ] || [ "$results" = yes ] || { say "nothing to delete in workspace $NEVIA_WORKSPACE"; return 0; }
  echo "In Nevia workspace $NEVIA_WORKSPACE, this deletes:"
  [ -z "$computer" ] || echo "  - on computer $LAB: ~/m2p (the tools, the runs) and the source (Rocket.Chat, its MongoDB and data)"
  for d in $dbs; do echo "  - database $d"; done
  echo "Kept: computer $LAB, its Nevia key and Claude Code's secrets, shared with uc2 and uc3 (../computer.sh destroy)"
  if [ "$results" = yes ]; then echo "On this machine: results/ (every run's report, the mongo2pg issues)"
  else echo "Kept: results/ on this machine (destroy --results deletes it too)"; fi
  confirm destroy "$yes" "This cannot be undone."
  if [ -n "$computer" ]; then
    say "deleting ~/m2p and the source on $LAB"
    nv exec -n "$LAB" -- sh -c 'if command -v podman >/dev/null; then
        podman rm -f rc-app rc-mongo >/dev/null 2>&1 || true; podman volume rm -f rc-mongo-data >/dev/null 2>&1 || true
      fi; rm -rf ~/m2p ~/rocketchat'
  fi
  # The branches first, then their parent.
  for d in $(printf '%s\n' $dbs | sort -r); do say "deleting database $d"; nv --yes pg rm "$d" --wait >/dev/null; done
  [ "$results" = no ] || { say "deleting results/"; rm -rf "$DIR/results"; }
  say "deleted; ./rc-lab.sh setup starts again"
}

case $CMD in
  all)     setup; if [ -n "$SOURCE" ]; then remote source up; lab source seed; lab analyse; else remote analyse; fi; migrate ;;
  sleep)   agent_sleep ;;
  setup)   setup ;;
  up|seed) [ -n "$SOURCE" ] || die "$CMD: no SOURCE in .env (an existing MongoDB has nothing to set up)"
           remote source up; [ "$CMD" = up ] || lab source seed ;;
  analyse) remote analyse ;;
  migrate) migrate ;;
  logs)    logs "${2:-}" ;;
  stop)    stop ;;
  status)  status ;;
  destroy) shift; destroy "$@" ;;
esac
