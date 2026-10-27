#!/bin/sh
# Step 4, on the lab computer (detached by lab.sh): clean slate, then Claude migrates the
# database to the Nevia Postgres with a generic prompt (task.txt), on branches, until its own
# checks pass on the main database; its REPORT.md ends with the verdict.
#   sh migrate/run.sh <run>
STEP_CMD="run ${1:-}"
. "$(dirname "$0")/../setup/env.sh"
RUN=${1:?run id}
HERE=$SCRIPTS/migrate

# set_target: the target of the project's config, the URI of the main database (new at each run).
set_target() {
  uri=$(nv pg connect --uri "$PG_NAME")
  (umask 077 && awk -v uri="$uri" '
    /^\[/ { sect = $0 }
    sect == "[target]" && /^uri[ \t]*=/ { print "uri = \"" uri "\""; next }
    { print }' "$CONFIG" > "$CONFIG.new") && mv "$CONFIG.new" "$CONFIG"
  unset uri
}

# clean <run>: nothing left of earlier runs. Their branches deleted, the main database recreated
# empty, the mongo2pg project back to what analyse made (analysed again if analyse changed since,
# or if the source no longer holds the documents it counted), the work directory and their files
# gone.
clean() {
  for db in $(nv --json pg ls | jq -r --arg p "$PG_NAME-" '[.. | objects | select(has("name")) | .name | select(startswith($p))] | unique[]'); do
    say "deleting branch $db"; nv --yes pg rm "$db" --wait >/dev/null
  done
  if nv pg status "$PG_NAME" >/dev/null 2>&1; then
    say "deleting $PG_NAME"; nv --yes pg rm "$PG_NAME" --wait >/dev/null
  fi
  say "creating $PG_NAME, empty"
  nv pg new "$PG_NAME" --wait >/dev/null
  rm -rf "$PROJECT" "$WORK" "$LAB/attempts"
  mkdir -p "$WORK"
  if [ ! -f "$PRISTINE" ] || [ "$(cat "$PRISTINE.sum" 2>/dev/null)" != "$(analyse_sum)" ]; then
    say "no snapshot of the mongo2pg project made by this analyse step: analysing again"
    sh "$SCRIPTS/analyse/analyse.sh"
  elif [ "$(counts)" != "$(cat "$COUNTS" 2>/dev/null)" ]; then
    say "the source changed since analyse (documents per collection): analysing again"
    sh "$SCRIPTS/analyse/analyse.sh"
  else
    say "mongo2pg project restored as analyse made it"
    tar -xzf "$PRISTINE" -C "$LAB/migration"
    set_target
  fi
  for f in "$LAB"/runs/*; do
    case $(basename "$f") in "$1".*) ;; *) rm -f "$f" ;; esac
  done
}

# fill: a file of migrate/ with the run's paths in it.
fill() {
  sed -e "s|{{MONGO_DB}}|$MONGO_DB|g" -e "s|{{PROJECT}}|$PROJECT|g" -e "s|{{CONFIG}}|$CONFIG|g" \
      -e "s|{{SCHEMA}}|$NAME|g" -e "s|{{WORK}}|$WORK|g" -e "s|{{PG_NAME}}|$PG_NAME|g" "$1"
}

# tools: the generic tools Claude starts with in the work directory, ready to run (they know
# mongo2pg and its project, not the data): the profile of the source, the verification, the
# fields no mapping covers, the mapping and data fixes of mongo2pg issues met before.
tools() {
  for f in profile.mjs verify.mjs unmapped.mjs fix-mappings.mjs precheck.mjs fix-data.sql; do fill "$HERE/$f" > "$WORK/$f"; done
}

# The tools Claude may use without asking; read-only shell commands (cat, grep, find...) pass anyway.
# No nevia: nvpg only, which touches $PG_NAME and its branches. srcmongo: the source database.
ALLOWED=${CLAUDE_ALLOWED_TOOLS:-Read,Edit,Write,Glob,Grep,Bash(mongo2pg:*),Bash(nvpg:*),Bash(psql:*),Bash(pg_isready:*),Bash(srcmongo:*),Bash(node:*),Bash(npm:*),Bash(python3:*),Bash(sed:*),Bash(zcat:*),Bash(gunzip:*),Bash(diff:*),Bash(jq:*),Bash(sleep:*),Bash(echo:*),Bash(mkdir:*),Bash(cp:*),Bash(awk:*),Bash(wc:*),Bash(sort:*),Bash(uniq:*),Bash(cut:*),Bash(tr:*),Bash(head:*),Bash(tail:*),Bash(cat:*),Bash(ls:*),Bash(grep:*)}
# What Claude may not do even if a rule above allows it: read the project's configs (config/*.toml
# and the branches' config/branches/*.toml), which hold the source and target URIs with their
# passwords. A Read rule also covers Grep and Glob; the Bash rules cover the usual ways to print a
# file (not every one: the task forbids it too).
DENIED="Read(/$PROJECT/config/**)"
# mongo2pg import into a branch, with its config: yes. Into the main database (the project's
# config, or the one nvpg config writes for it): no, only nvpg import main, once a branch passed.
DENIED="$DENIED,Bash(mongo2pg import*config/$NAME.toml*),Bash(mongo2pg import*config/branches/$PG_NAME.toml*)"
for c in cat head tail less more grep sed awk; do DENIED="$DENIED,Bash($c $PROJECT/config:*)"; done
# What one command may put in Claude's context: every token of it is read again at each later turn.
BASH_MAX_OUTPUT_LENGTH=${CLAUDE_BASH_MAX_OUTPUT:-10000}

echo $$ > "$LAB/runs/$RUN.pid"
key_on
command -v claude >/dev/null || die "Claude Code not found on this computer"
[ -s "$HOME/.claude/.credentials.json" ] || [ -n "${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_AUTH_TOKEN:-}" ] ||
  die "Claude Code is not logged in: nevia shell <computer>, then claude and /login (or ANTHROPIC_AUTH_TOKEN with ANTHROPIC_BASE_URL, CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in .env, then ./lab.sh setup)"
[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] || say "Claude Code through the gateway ${ANTHROPIC_BASE_URL:-(no ANTHROPIC_BASE_URL)}, model $CLAUDE_MODEL"

step "1/3 preparing: MongoDB, the tools, nothing left of earlier runs"
install
mongo_up
srcmongo --eval 'db.runCommand({ ping: 1 }).ok' >/dev/null || die "MongoDB not reachable at MONGO_URI (database $MONGO_DB)"
clean "$RUN"
tools
fill "$HERE/task.txt" > "$LAB/runs/$RUN.task.txt"
nv pg awake "$PG_NAME" --wait >/dev/null 2>&1 || true
say "Claude Code $(claude --version 2>/dev/null), $(mongo2pg --version), $(psql --version)"

step "2/3 Claude Code on the migration of $MONGO_DB"
cd "$WORK"
CLAUDE_START=$(date +%s)
# Its events printed one line each as they come, with the time since the run's start, and any
# password of a URI (postgres://user:password@...) masked.
# Claude Code with its built-in tools only: no MCP server (--strict-mcp-config), no claude.ai
# connector of the account it is logged in with (ENABLE_CLAUDEAI_MCP_SERVERS), no plugin, skill or
# agent of the user's settings (--setting-sources): their definitions came to ~180K tokens, read
# again at every turn, and the connectors (mail, chat, drive...) have no place in the lab.
{ ENABLE_CLAUDEAI_MCP_SERVERS=false BASH_MAX_OUTPUT_LENGTH=$BASH_MAX_OUTPUT_LENGTH \
    claude -p "$(cat "$LAB/runs/$RUN.task.txt")" \
    --output-format stream-json --verbose --max-turns "${CLAUDE_MAX_TURNS:-250}" \
    --permission-mode acceptEdits --tools "Read,Edit,Write,Glob,Grep,Bash" \
    --strict-mcp-config --setting-sources project,local \
    --allowedTools "$ALLOWED" --disallowedTools "$DENIED" --model "$CLAUDE_MODEL" \
    --add-dir "$LAB/migration" --add-dir /tmp \
    2>> "$LAB/runs/$RUN.claude.log" || tail -20 "$LAB/runs/$RUN.claude.log" >&2; } |
  tee "$LAB/runs/$RUN.stream.jsonl" |
  jq -nR --unbuffered -r --argjson t0 "$START" '
    def redact: gsub("(?<s>[a-z+]+://)[^/@\\s]+@"; "\(.s)<redacted>@");
    def short: tostring | redact | split("\n")[0] | .[0:160];
    def el: (now - $t0 | floor) | "+\(. / 60 | floor)m\(. % 60 | tostring | if length < 2 then "0" + . else . end)s";
    inputs | fromjson? |
    if .type == "system" and .subtype == "init" then
      "[claude \(el)] started: \(.model), \(.tools | length) tools, \(.mcp_servers // [] | length) MCP servers"
    elif .type == "assistant" then
      .message.content[]? |
      if .type == "tool_use" then "[claude \(el)] \(.name) \(.input | .file_path // .pattern // .command // .path // "" | short)"
      elif .type == "text" and (.text | test("\\S")) then "[claude \(el)] » \(.text | short)"
      else empty end
    elif .type == "user" then
      .message.content[]? | select(.type == "tool_result" and .is_error == true) |
      "[claude \(el)]   ✗ \(.content | if type == "array" then (map(.text? // "") | join(" ")) else . end | short)"
    else empty end'
grep '"type":"result"' "$LAB/runs/$RUN.stream.jsonl" | tail -n 1 > "$LAB/runs/$RUN.result.json" || true
[ -s "$LAB/runs/$RUN.result.json" ] || die "Claude Code ended without a result"
# What it started with: the tool definitions are read again at every turn.
init=$(grep '"subtype":"init"' "$LAB/runs/$RUN.stream.jsonl" | head -n 1)
[ -z "$init" ] || [ "$(printf '%s' "$init" | jq '.mcp_servers // [] | length')" -eq 0 ] ||
  say "warning: Claude Code loaded MCP servers ($(printf '%s' "$init" | jq -r '[.mcp_servers[].name] | join(", ")' | cut -c1-300)): every turn paid for their tools"

step "3/3 result"
# For the final report: the timing, the branches, the run's facts (no secret in them).
jq -s -r --argjson t0 "$CLAUDE_START" -f "$HERE/timing.jq" "$LAB/runs/$RUN.stream.jsonl" > "$LAB/runs/$RUN.timing.txt" || true
sed "s/^/[$TAG] ⏱ /" "$LAB/runs/$RUN.timing.txt"
# What it cost: in all, per model, per attempt, per document migrated (cost.jq).
jq -s --slurpfile result "$LAB/runs/$RUN.result.json" --argjson docs "$(jq 'add // 0' "$COUNTS" 2>/dev/null || echo 0)" \
  -f "$HERE/cost.jq" "$LAB/runs/$RUN.stream.jsonl" > "$LAB/runs/$RUN.cost.json" || true
say "Claude: $(jq -r '"\(.num_turns // "?") turns, \(((.duration_ms // 0) / 1000) | floor) s, $\((.total_cost_usd // 0) * 100 | round / 100)"' "$LAB/runs/$RUN.result.json")"
say "tokens: $(jq -r '(.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)}) | "input \(.input), output \(.output), cache write \(.cache_write), cache read \(.cache_read)"' "$LAB/runs/$RUN.result.json")"
jq -r '.result // .subtype' "$LAB/runs/$RUN.result.json"
nvpg ls | tee "$LAB/runs/$RUN.branches.txt"
jq -n --arg run "$RUN" --arg db "$MONGO_DB" --arg schema "$NAME" --arg pg "$PG_NAME" \
  --arg claude "$(claude --version 2>/dev/null)" --arg m2p "$(mongo2pg --version)" --arg psql "$(psql --version)" \
  --argjson started "$START" --argjson claude_started "$CLAUDE_START" --argjson ended "$(date +%s)" \
  --slurpfile result "$LAB/runs/$RUN.result.json" \
  '{run: $run, mongo_db: $db, schema: $schema, pg_name: $pg, started: $started, claude_started: $claude_started,
    ended: $ended, versions: {claude: $claude, mongo2pg: $m2p, psql: $psql},
    model: ($result[0].modelUsage // {} | keys | join(", ")), turns: $result[0].num_turns,
    cost_usd: $result[0].total_cost_usd, is_error: $result[0].is_error,
    tokens: ($result[0] | (.modelUsage // {} | [.[]] | {input: (map(.inputTokens // 0) | add // 0), output: (map(.outputTokens // 0) | add // 0), cache_write: (map(.cacheCreationInputTokens // 0) | add // 0), cache_read: (map(.cacheReadInputTokens // 0) | add // 0)}))}' > "$LAB/runs/$RUN.meta.json" || true
jq -e '.is_error | not' "$LAB/runs/$RUN.result.json" >/dev/null || die "Claude Code failed"
# The verdict is Claude's, the last line of its report (the report's header shows it).
[ -s "$WORK/REPORT.md" ] || die "no REPORT.md"
verdict=$(tail -n 1 "$WORK/REPORT.md")
say "${verdict:-no verdict}"
case $verdict in 'VERDICT: PASS'*) ;; *) die "the migration is not verified: ${verdict:-no VERDICT line in REPORT.md}" ;; esac
# And verify.mjs's own result on the main database, when Claude ran it: a PASS needs it too.
if [ -s "$WORK/verify-main.json" ]; then
  jq -e '.target == "main" and .fail == 0 and .pass > 0' "$WORK/verify-main.json" >/dev/null 2>&1 ||
    die "Claude's verdict is PASS, but verify.mjs on the main database: $(jq -r '"\(.pass) PASS, \(.fail) FAIL"' "$WORK/verify-main.json")"
fi
