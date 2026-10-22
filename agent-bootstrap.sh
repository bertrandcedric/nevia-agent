#!/bin/sh
# Runs ON the agent computer, sent by agent.sh through nevia exec:
#   sh -s ls | preview <run> | cleanup <run> | sleep [<delay>]   script on stdin, from agent.sh
#   sh <run>.sh run <run> <task-b64>                      stored in ~/agent/runs, detached
# Every command but run ends by putting this computer to sleep, unless a run is going; agent.sh
# starts `sleep` once a run is done.
#
# The workflow of run:
#   1. get the GitHub repo (clone the base branch), check the token can push
#   2. launch the database and the application with the app's own deploy.sh, unless
#      it already runs and passes its checks
#   3. test the application (deploy.sh check); a broken base stops here
#   4. Claude Code evaluates the task and implements it, on the code only (build with Maven,
#      no access to Nevia or GitHub); no change, or a broken build, stops here
#   5. test the new code: checkpoint the application, restore it into a sandbox, deploy the new
#      code there with deploy.sh in sandbox mode (it gives the sandbox a database of its own,
#      according to the change's `database` file), run the checks
#   6. push a branch and open a PR with the results and the sandbox as preview
# Claude never runs git or nevia: git, GitHub and the Nevia resources are handled by this script.
#
# Env: stored on the computer (agent.sh setup): GITHUB_TOKEN, AGENT_NEVIA_KEY; given at each exec
# (from agent.sh's .env): AGENT, AGENT_NEVIA_WORKSPACE, REPO, BASE_BRANCH, BASE_COMPUTER,
# CLAUDE_MODEL (optional).
set -eu

JDK_MAJOR=21
MAVEN_VERSION=3.9.12

BASE=$HOME/agent
mkdir -p "$BASE/log" "$BASE/tmp" "$BASE/runs"

say() { echo "[agent] $*"; }
step() { printf '\n\033[1m[agent] ─── %s ───\033[0m\n' "$*"; }
die() { echo "[agent] error: $*" >&2; exit 1; }

[ -n "${AGENT_NEVIA_KEY:-}" ] || die "missing env AGENT_NEVIA_KEY: ./agent.sh setup"
for v in AGENT AGENT_NEVIA_WORKSPACE; do
  eval "[ -n \"\${$v:-}\" ]" || die "missing env $v: start this through agent.sh"
done
CMD=${1:-}
BASE_BRANCH=${BASE_BRANCH:-main}
BASE_COMPUTER=${BASE_COMPUTER:-hello-app}

# The Nevia default image ships the nevia CLI and Claude Code.
command -v nevia >/dev/null || die "nevia CLI not found on this computer"

# The agent's own Nevia identity (editor key, see agent.sh setup). Kept in a shell variable
# that children don't inherit; the key file (readable by this user only, one per process)
# exists only while this script needs nevia, not while Claude runs.
NEVIA_KEY=$AGENT_NEVIA_KEY
unset AGENT_NEVIA_KEY
# Drop the key files of processes that are gone (killed while holding one).
for f in "$BASE"/tmp/nevia-key.*; do
  [ -e "$f" ] || continue
  kill -0 "${f##*.}" 2>/dev/null || rm -f "$f"
done
KEY_FILE=$BASE/tmp/nevia-key.$$
key_on() { (umask 077 && printf '%s\n' "$NEVIA_KEY" > "$KEY_FILE"); }
key_off() { rm -f "$KEY_FILE"; }
# A computer's stored env can't use the reserved NEVIA_* names, so the CLI's ones are set here.
export NEVIA_KEY_FILE=$KEY_FILE NEVIA_WORKSPACE=$AGENT_NEVIA_WORKSPACE
trap key_off EXIT
key_on
nv() { nevia --non-interactive "$@"; }
nv whoami >/dev/null 2>&1 || die "the agent's Nevia key is not valid: run ./agent.sh setup --rotate"

# status_json <computer>: the computer as JSON, nothing if it doesn't exist (exit 3).
status_json() {
  rc=0; out=$(nv --json status "$1") || rc=$?
  case $rc in
    0) printf '%s' "$out" ;;
    3) ;;
    *) printf '%s\n' "$out" >&2; die "nevia status $1 failed (exit $rc)" ;;
  esac
}
json_field() { printf '%s' "$1" | jq -r --arg k "$2" 'first(.. | objects | select(has($k)) | .[$k]) // empty'; }

# sandbox_names <run>: the names of a run's sandbox.
sandbox_names() {
  case ${1:-} in ''|*[!0-9]*) die "a run id is digits only, got '${1:-}'" ;; esac
  CHECKPOINT=agent-$1 SANDBOX_COMPUTER=$BASE_COMPUTER-$1
}

# The resources of a run's sandbox: whatever deploy.sh created for it is named <name>-<run>
# (computers, Postgres databases) or <NAME>_<run> (secrets), so they are found by that suffix.
run_computers() { nv --json ls | jq -r --arg s "-$1" '.computers[].name | select(endswith($s))'; }
run_databases() { nv --json pg ls | jq -r --arg s "-$1" '[.. | objects | select(has("name")) | .name] | unique[] | select(endswith($s))'; }
run_secrets() { nv --json secrets ls | jq -r --arg s "_$1" '.secrets[].name | select(endswith($s))'; }

# going: whether a run is going (a live process that has written no status yet).
going() {
  for f in "$BASE"/runs/*.pid; do
    [ -e "$f" ] || continue
    if [ ! -e "${f%.pid}.status" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
      say "run $(basename "$f" .pid) is going: $AGENT stays awake"
      return 0
    fi
  done
  return 1
}

# sleep_if_idle [<delay>]: put this computer to sleep <delay> seconds from now (3 by default, so
# that the exec calling this returns first), unless a run is going then.
sleep_if_idle() {
  going && return 0
  say "putting $AGENT to sleep in ${1:-3} s"
  id=$(nv --json status "$AGENT" | jq -r '.computer.id // empty')
  [ -n "$id" ] || die "no id for $AGENT"
  token=$(nv auth access-token)
  # Sleep freezes processes and wakes resume them, so a pending sleep must not fire after a wake:
  # one pending sleep at a time (the new one replaces it), dropped if the clock jumped (this
  # computer slept meanwhile) or a run started; then one request, no retry (a `nevia suspend`
  # frozen mid-call retries once woken). The token (valid about 15 minutes) goes through a pipe,
  # no key file is left on disk.
  [ ! -f "$BASE/tmp/sleep.pid" ] || kill "$(cat "$BASE/tmp/sleep.pid")" 2>/dev/null || true
  export BASE AGENT
  printf '%s\n' "$token" | setsid sh -c '
    echo $$ > "$BASE/tmp/sleep.pid"
    read -r t; t0=$(date +%s); sleep "$2"
    [ $(($(date +%s) - t0)) -le $(($2 + 5)) ] || exit 0
    for f in "$BASE"/runs/*.pid; do
      [ -e "$f" ] && [ ! -e "${f%.pid}.status" ] && kill -0 "$(cat "$f")" 2>/dev/null && exit 0
    done
    curl -fsS --max-time 20 -X POST -H "Authorization: Bearer $t" "https://api.nevia.cloud/v1/computers/$1/sleep"' \
    sh "$id" "${1:-3}" > /dev/null 2>&1 &
}

case $CMD in
  sleep)
    sleep_if_idle "${2:-3}"; exit 0 ;;
  ls)
    nv --json ls | jq -r --arg p "$BASE_COMPUTER-" '
      .computers[] | select(.name | startswith($p)) | select(.name | ltrimstr($p) | test("^[0-9]+$"))
      | "\(.name | ltrimstr($p))\t\(.name)\t\(.state)\t\(first(.ports[]?.url) // "-")"' |
      { printf 'RUN\tCOMPUTER\tSTATE\tURL\n'; cat; }
    sleep_if_idle; exit 0 ;;
  preview)
    sandbox_names "${2:-}"
    json=$(status_json "$SANDBOX_COMPUTER")
    [ -n "$json" ] || die "no sandbox for run $2"
    # The database first: the app retries it at startup, not the other way round.
    for d in $(run_databases "$2"); do say "waking $d"; nv pg awake "$d" --wait >/dev/null 2>&1 || true; done
    for c in $(run_computers "$2" | grep -vx "$SANDBOX_COMPUTER") "$SANDBOX_COMPUTER"; do
      say "waking $c"; nv awake "$c" --wait >/dev/null 2>&1 || true
    done
    say "preview: $(json_field "$json" url)"
    sleep_if_idle; exit 0 ;;
  cleanup)
    # Idempotent; the computer goes first: a checkpoint can't be deleted while a computer
    # restored from it is live.
    sandbox_names "${2:-}"
    if [ -n "$(status_json "$SANDBOX_COMPUTER")" ]; then
      say "deleting computer $SANDBOX_COMPUTER"; nv --yes rm "$SANDBOX_COMPUTER" --wait >/dev/null
    fi
    for c in $(run_computers "$2"); do say "deleting computer $c"; nv --yes rm "$c" --wait >/dev/null; done
    for d in $(run_databases "$2"); do say "deleting database $d"; nv --yes pg rm "$d" --wait >/dev/null; done
    for s in $(run_secrets "$2"); do say "deleting secret $s"; nv --yes secrets unset "$s" >/dev/null; done
    if nv checkpoint status -c "$BASE_COMPUTER" "$CHECKPOINT" >/dev/null 2>&1; then
      say "deleting checkpoint $CHECKPOINT"; nv --yes checkpoint rm -c "$BASE_COMPUTER" "$CHECKPOINT" >/dev/null
    fi
    say "run $2 cleaned up"
    sleep_if_idle; exit 0 ;;
  run) ;;
  *) die "unknown command '$CMD'" ;;
esac

# --- run <run> <task-b64> -------------------------------------------------------------
RUN=${2:-}
sandbox_names "$RUN"
TASK=$(printf '%s' "${3:-}" | base64 -d)
case $TASK in *[![:space:]]*) ;; *) die "no task: agent.sh run \"<task>\"" ;; esac
[ -n "${GITHUB_TOKEN:-}" ] || die "missing env GITHUB_TOKEN: ./agent.sh setup"
[ -n "${REPO:-}" ] || die "missing env REPO: start this through agent.sh"
[ "$(uname -m)" = x86_64 ] || die "this script handles x86_64 only, got $(uname -m)"
# sleep_if_idle sees this run as going while this process lives and no status is written.
echo $$ > "$BASE/runs/$RUN.pid"

fetch() { curl -fsSL "$1" -o "$2"; }

# --- install (JDK/Maven: same steps as the app's bootstrap.sh) ---------------------------
step "installing tools (first run only)"
if [ ! -x "$BASE/jdk/bin/java" ]; then
  meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$JDK_MAJOR/hotspot?os=linux&architecture=x64&image_type=jdk")
  link=$(printf '%s' "$meta" | jq -r '.[0].binary.package.link')
  sum=$(printf '%s' "$meta" | jq -r '.[0].binary.package.checksum')
  say "downloading $link"
  fetch "$link" "$BASE/tmp/jdk.tgz"
  echo "$sum  $BASE/tmp/jdk.tgz" | sha256sum -c - >/dev/null || die "JDK checksum mismatch"
  mkdir -p "$BASE/jdk"
  tar -xzf "$BASE/tmp/jdk.tgz" -C "$BASE/jdk" --strip-components=1
  rm -f "$BASE/tmp/jdk.tgz"
fi

if [ ! -x "$BASE/maven/bin/mvn" ]; then
  url="https://archive.apache.org/dist/maven/maven-3/$MAVEN_VERSION/binaries/apache-maven-$MAVEN_VERSION-bin.tar.gz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/maven.tgz"
  echo "$(curl -fsSL "$url.sha512" | cut -d' ' -f1)  $BASE/tmp/maven.tgz" | sha512sum -c - >/dev/null || die "Maven checksum mismatch"
  mkdir -p "$BASE/maven"
  tar -xzf "$BASE/tmp/maven.tgz" -C "$BASE/maven" --strip-components=1
  rm -f "$BASE/tmp/maven.tgz"
fi
export JAVA_HOME=$BASE/jdk PATH=$BASE/maven/bin:$BASE/jdk/bin:$PATH
command -v claude >/dev/null || die "Claude Code not found on this computer"
say "Claude Code $(claude --version 2>/dev/null)"

# --- 1. get the GitHub repo -------------------------------------------------------
# The token comes from the env at each call and is never written into .git/config.
git_() {
  git -c credential.helper= \
      -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' "$@"
}

step "1/6 cloning $REPO ($BASE_BRANCH)"
rm -rf "$BASE/repo"
git_ clone -q --branch "$BASE_BRANCH" "https://github.com/$REPO.git" "$BASE/repo"
cd "$BASE/repo"
git config user.name "nevia-agent"
git config user.email "nevia-agent@users.noreply.github.com"
[ -x deploy.sh ] || die "no executable deploy.sh at the root of $REPO ($BASE_BRANCH)"
# Fail now rather than after the agent's work: a dry-run push goes through GitHub's write
# authorization without creating anything.
git_ push -q --dry-run origin "HEAD:refs/heads/agent/$RUN" 2>/dev/null ||
  die "GITHUB_TOKEN can clone $REPO but not push: give the token Contents and Pull requests: Read and write"

# logged <log> <cmd>...: run cmd, show and save its output, return its exit code (the app's
# deploy.sh exits 0 only when its checks pass; a pipe into tee would hide that code).
logged() {
  log=$1; shift
  { if "$@"; then rc=0; else rc=$?; fi; echo "$rc" > "$BASE/tmp/rc"; } 2>&1 | tee "$log"
  return "$(cat "$BASE/tmp/rc")"
}

# --- 2+3. launch the database and the application, test them --------------------
# An app that already runs and passes its checks is kept as is (no rebuild, no restart): the
# sandbox gets the new code anyway. Otherwise (missing, asleep, failing) it is deployed.
step "2/6 launching the app on $BASE_COMPUTER, 3/6 testing it"
if COMPUTER=$BASE_COMPUTER logged "$BASE/log/base.log" ./deploy.sh check; then
  say "$BASE_COMPUTER already runs and passes its checks: not redeployed"
  BASE_STATE="already running"
else
  say "$BASE_COMPUTER is not running or fails its checks: deploying $BASE_BRANCH"
  COMPUTER=$BASE_COMPUTER logged "$BASE/log/base.log" ./deploy.sh deploy ||
    die "$BASE_BRANCH does not pass its checks on $BASE_COMPUTER: no PR"
  BASE_STATE="\`$BASE_BRANCH\` deployed"
fi

# --- 4. Claude: evaluate the task, implement it (code only) -------------------------
step "4/6 Claude Code on the task: $(printf '%s' "$TASK" | head -c 100)"
PROMPT="You are working in a clone of $REPO. Your task:

$TASK

Context:
- The application is a Maven project (pom.xml).
- The file \`database\` (one word: mongo or postgres) tells deploy.sh which database to give the
  application, and the environment variables it gets (see README.md). If the task changes the
  database, change that file along with the code.
- You work on the code only. After you, the pipeline deploys your change to a sandbox (a copy of
  the application and of its database) and checks GET /healthz and GET /; if that fails, no PR.

Steps:
1. Evaluate the task: is it clear, feasible and in the scope of this project? If not, change
   no file and explain why.
2. Implement the smallest change that solves it, in the style of the surrounding code.
3. Validate it with \`mvn -q -B package\` (add or adapt tests when it makes sense).

Rules:
- Do not run git. Do not modify deploy.sh, bootstrap.sh or mongo/bootstrap.sh.
- Your final reply is the pull request, in exactly this form and nothing else:
  <title>a short imperative title, under 72 characters, e.g. Reject non-GET requests on /</title>
  <body>GitHub markdown with the sections \"## Task evaluation\", \"## Solution\" and \"## Tests\"
  (the commands you ran and what they returned)</body>"

# Auth: the login done once on this computer.
[ -s "$HOME/.claude/.credentials.json" ] ||
  say "warning: no Claude Code login found in ~/.claude; if it fails: nevia shell, then claude and /login"

# Claude has neither the GitHub token nor the Nevia key, can only build with Maven, and the
# deploy scripts are off-limits for edits.
key_off
env -u GITHUB_TOKEN -u NEVIA_KEY_FILE -u NEVIA_WORKSPACE \
  claude -p "$PROMPT" --output-format json --max-turns 60 \
    --permission-mode acceptEdits \
    --allowedTools "Read,Edit,Write,Glob,Grep,Bash(mvn:*)" \
    --disallowedTools "Edit(./deploy.sh),Write(./deploy.sh),Edit(./bootstrap.sh),Write(./bootstrap.sh),Edit(./mongo/bootstrap.sh),Write(./mongo/bootstrap.sh)" \
    ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} \
    > "$BASE/tmp/result.json" 2>> "$BASE/log/claude.log" ||
  tail -20 "$BASE/log/claude.log" >&2
key_on

# With --output-format json, Claude Code reports its errors (e.g. an expired login) in the
# result on stdout: checked here, along with the reply.
# pr.md = title on the first line, then the body. From the <title>/<body> tags the prompt asks
# for; without them, the first line of the reply is the title.
jq -r '
  def trim: gsub("^\\s+|\\s+$"; "");
  def tag($t): [match("<\($t)>([\\s\\S]*?)</\($t)>").captures[0].string][0] // "" | trim;
  if .is_error then
    "Claude Code: \(.result // .subtype)\(if .api_error_status then " (API status \(.api_error_status))" else "" end)\n" | halt_error(1)
  else . end
  | (.result // "" | trim) as $text
  | ($text | tag("title")) as $title
  | if $title != "" then [$title, ($text | tag("body"))]
    else ($text | split("\n")) as $l | [$l[0], ($l[1:] | join("\n") | trim)] end
  | "\(.[0] | sub("^#+\\s*"; "") | gsub("\\s+"; " ") | .[0:120])\n\n\(.[1])"
' "$BASE/tmp/result.json" > "$BASE/tmp/pr.md" || die "Claude Code failed: no PR"

# Claude works silently (--output-format json gives one object at the end): its reply, the one
# that becomes the pull request, is printed here so the run's log shows what it decided.
jq -r '"\(.num_turns // "?") turns, \(((.duration_ms // 0) / 1000) | floor) s\(if .total_cost_usd then ", $\(.total_cost_usd * 100 | round / 100)" else "" end)"' \
  "$BASE/tmp/result.json" 2>/dev/null | while read -r l; do say "Claude done: $l"; done
say "Claude's proposal (becomes the pull request):"
sed 's/^/  /' "$BASE/tmp/pr.md"
echo

# Edits to the deploy scripts are refused above; drop any that slipped through anyway.
for p in deploy.sh bootstrap.sh mongo/bootstrap.sh; do
  if git ls-files --error-unmatch "$p" >/dev/null 2>&1; then git checkout -q -- "$p"; git clean -qfd -- "$p"; fi
done
if [ -z "$(git status --porcelain)" ]; then
  # The reply is printed above, whatever it says.
  say "no change proposed, no sandbox, no PR"
  exit 0
fi
step "building the change (mvn package)"
mvn -q -B package >> "$BASE/log/build.log" 2>&1 ||
  { tail -40 "$BASE/log/build.log" >&2; die "build fails with the agent's change: no sandbox, no PR"; }

# --- 5. test the new code: checkpoint of the app, sandbox deployed by the app's deploy.sh ---
step "5/6 testing the change: checkpoint $CHECKPOINT of $BASE_COMPUTER, sandbox $SANDBOX_COMPUTER"
nv checkpoint create -c "$BASE_COMPUTER" --name "$CHECKPOINT" --wait >/dev/null
nv restore "$CHECKPOINT" -c "$BASE_COMPUTER" --name "$SANDBOX_COMPUTER" --wait >/dev/null

# Right after a restore, the sandbox's network (DNS) may not answer yet; *.live.nevia.cloud
# is a wildcard, so a lookup that succeeds means the network is up. Wait until the sandbox
# resolves a host there several times in a row (the app also retries at startup).
host=$(json_field "$(status_json "$BASE_COMPUTER")" url | sed 's#^https://##; s#[:/].*##')
[ -n "$host" ] || die "no host for $BASE_COMPUTER (cleanup: ./agent.sh cleanup $RUN)"
say "waiting for $SANDBOX_COMPUTER to resolve $host"
i=0 ok=0
while [ $ok -lt 5 ]; do
  if nevia exec -n "$SANDBOX_COMPUTER" -- getent hosts "$host" >/dev/null 2>&1; then ok=$((ok + 1)); else ok=0; fi
  i=$((i + 1))
  [ $i -lt 120 ] || die "$SANDBOX_COMPUTER still does not resolve $host after ~5 min (cleanup: ./agent.sh cleanup $RUN)"
  sleep 2
done

# The app's deploy.sh in sandbox mode: gives the sandbox a database of its own (a branch of the
# stack's, or a new one when the change switches database), points the restored computer at it,
# pushes the working tree (the new code), restarts the app and checks it.
say "deploying the change on $SANDBOX_COMPUTER (SANDBOX=$RUN, database: $(tr -d '[:space:]' < database 2>/dev/null))"
COMPUTER=$SANDBOX_COMPUTER SANDBOX=$RUN \
  logged "$BASE/log/validation.log" ./deploy.sh deploy ||
  die "the change fails its checks on $SANDBOX_COMPUTER: no PR (sandbox kept: ./agent.sh cleanup $RUN)"
SANDBOX_URL=$(nv --json status "$SANDBOX_COMPUTER" | jq -r 'first(.. | objects | select(has("url")) | .url) // empty')

# --- 6. push the PR -------------------------------------------------------------------
step "6/6 opening the pull request"
TITLE=$(head -n 1 "$BASE/tmp/pr.md")
[ -n "$TITLE" ] || TITLE="Agent proposal"
BRANCH="agent/$RUN"
{
  printf '\n## Automated validation\n\n'
  printf -- '- `%s` checked before the change (%s).\n' "$BASE_COMPUTER" "$BASE_STATE"
  printf -- '- With this change, `mvn package` passes.\n'
  printf -- '- Sandbox: computer `%s` restored from checkpoint `%s`, database given by `deploy.sh` (`SANDBOX=%s`, `database`: `%s`).\n' \
    "$SANDBOX_COMPUTER" "$CHECKPOINT" "$RUN" "$(tr -d '[:space:]' < database)"
  printf -- '- `deploy.sh deploy` of this change on the sandbox returns:\n\n```\n'
  sed 's/\x1b\[[0-9;]*m//g' "$BASE/log/validation.log" | grep -v '^\[helloworld\]' | tail -8
  printf '```\n\n## Preview\n\n%s (running)\n\n```sh\n./agent.sh preview %s   # wake it if it was put to sleep\n./agent.sh cleanup %s   # delete it once the PR is merged or closed\n```\n' \
    "$SANDBOX_URL" "$RUN" "$RUN"
  printf '\n---\nProposed by Claude Code running on a Nevia computer.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n'
} >> "$BASE/tmp/pr.md"

git checkout -q -b "$BRANCH"
git add -A
git commit -q -m "$TITLE" -m "Proposed by Claude Code running on a Nevia computer (run $RUN)."
say "pushing $BRANCH"
git_ push -q origin "$BRANCH"

payload=$(tail -n +2 "$BASE/tmp/pr.md" | jq -Rs --arg title "$TITLE" --arg head "$BRANCH" --arg base "$BASE_BRANCH" \
  '{title: $title, head: $head, base: $base, body: (. | gsub("^\\s+|\\s+$"; ""))}')
url=$(curl -fsS -X POST "https://api.github.com/repos/$REPO/pulls" \
        -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
        -d "$payload" | jq -r .html_url) ||
  die "branch $BRANCH pushed but the pull request could not be created"

# The sandbox stays up as the PR's live preview, until ./agent.sh cleanup.
say "preview: $SANDBOX_URL (sandbox $SANDBOX_COMPUTER, running)"
say "pull request: $url"
