#!/bin/sh
# Runs ON the agent computer, piped through `nevia exec <agent> -- sh -s <run> <task-b64>`
# by agent.sh. The workflow:
#   1. get the GitHub repo (clone the base branch), check the token can push
#   2. launch the database and the application with the app's own deploy.sh ($APP_DIR), unless
#      it already runs and passes its checks
#   3. test the application (deploy.sh check); a broken base stops here
#   4. Claude Code evaluates the task and implements it, on the code only (build with Maven,
#      no access to Nevia or GitHub); no change, or a broken build, stops here
#   5. test the new code: checkpoint the application and branch the database, restore the
#      checkpoint into a sandbox wired to the branch, deploy the new code there, run the checks
#   6. push a branch and open a PR with the results and the sandbox as preview
# Claude never runs git or nevia: git, GitHub and the Nevia resources are handled by this script.
#
# Env (agent computer's stored env, see agent.sh): GITHUB_TOKEN, AGENT_NEVIA_KEY, AGENT_NEVIA_WORKSPACE, REPO, BASE_BRANCH, APP_DIR, BASE_COMPUTER, BASE_DATABASE,
# CLAUDE_MODEL (optional).
set -eu

JDK_MAJOR=21
MAVEN_VERSION=${MAVEN_VERSION:-3.9.12}

BASE=$HOME/agent
mkdir -p "$BASE/log" "$BASE/tmp"

say() { echo "[agent] $*"; }
step() { printf '\n\033[1m[agent] == %s\033[0m\n' "$*"; }
die() { echo "[agent] error: $*" >&2; exit 1; }

[ "$(uname -m)" = x86_64 ] || die "this script handles x86_64 only, got $(uname -m)"
for v in GITHUB_TOKEN AGENT_NEVIA_KEY AGENT_NEVIA_WORKSPACE REPO; do
  eval "[ -n \"\${$v:-}\" ]" || die "missing env $v"
done

RUN=${1:?usage: agent-bootstrap.sh <run> <task-b64>}
TASK=$(printf '%s' "${2:-}" | base64 -d)
case $TASK in *[![:space:]]*) ;; *) die "no task: agent.sh run \"<task>\"" ;; esac

BASE_BRANCH=${BASE_BRANCH:-main}
BASE_COMPUTER=${BASE_COMPUTER:-hello-app}
BASE_DATABASE=${BASE_DATABASE:-hello-pg}
# The app's folder in the repo (. for the root). The agent only relies on its interface:
# deploy.sh (COMPUTER, PG_DATABASE, SECRET_DATABASE_URL pick the stack) and pom.xml.
APP_DIR=${APP_DIR:-.}
# app_path <file>: the file's path from the repo root, without a leading ./ (permission rules).
app_path() { if [ "$APP_DIR" = . ]; then echo "$1"; else echo "${APP_DIR%/}/$1"; fi; }
DEPLOY=$(app_path deploy.sh)
APP_BOOTSTRAP=$(app_path bootstrap.sh)
POM=$(app_path pom.xml)
# Sandbox names; agent.sh preview/cleanup derive the same ones from the run id.
CHECKPOINT=agent-$RUN
SANDBOX_COMPUTER=$BASE_COMPUTER-$RUN
SANDBOX_DATABASE=$BASE_DATABASE-$RUN
SANDBOX_SECRET=AGENT_DATABASE_URL_$RUN

fetch() { curl -fsSL "$1" -o "$2"; }

# --- install (JDK/Maven: same steps as the app's bootstrap.sh) ---------------------------
step "installing tools (first run only)"
if [ ! -x "$BASE/jdk/bin/java" ]; then
  meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$JDK_MAJOR/hotspot?os=linux&architecture=x64&image_type=jdk")
  link=$(printf '%s' "$meta" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s)[0].binary.package.link))')
  sum=$(printf '%s' "$meta" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s)[0].binary.package.checksum))')
  say "downloading $link"
  fetch "$link" "$BASE/tmp/jdk.tgz"
  echo "$sum  $BASE/tmp/jdk.tgz" | sha256sum -c - >/dev/null || die "JDK checksum mismatch"
  mkdir -p "$BASE/jdk"
  tar -xzf "$BASE/tmp/jdk.tgz" -C "$BASE/jdk" --strip-components=1
  rm -rf "$BASE"/tmp/*
fi

if [ ! -x "$BASE/maven/bin/mvn" ]; then
  url="https://archive.apache.org/dist/maven/maven-3/$MAVEN_VERSION/binaries/apache-maven-$MAVEN_VERSION-bin.tar.gz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/maven.tgz"
  echo "$(curl -fsSL "$url.sha512" | cut -d' ' -f1)  $BASE/tmp/maven.tgz" | sha512sum -c - >/dev/null || die "Maven checksum mismatch"
  mkdir -p "$BASE/maven"
  tar -xzf "$BASE/tmp/maven.tgz" -C "$BASE/maven" --strip-components=1
  rm -rf "$BASE"/tmp/*
fi

# The Nevia default image ships Claude Code (/usr/local/bin/claude); install it only on an
# image without it.
if ! command -v claude >/dev/null && [ ! -x "$BASE/npm/bin/claude" ]; then
  say "installing Claude Code"
  npm install -g --prefix "$BASE/npm" @anthropic-ai/claude-code >> "$BASE/log/install.log" 2>&1 ||
    { tail -20 "$BASE/log/install.log" >&2; die "Claude Code install failed"; }
fi

if [ ! -x "$HOME/.local/bin/nevia" ]; then
  say "installing the nevia CLI"
  curl -fsSL https://get.nevia.cloud | sh >> "$BASE/log/install.log" 2>&1 ||
    { tail -20 "$BASE/log/install.log" >&2; die "nevia CLI install failed"; }
fi
# $BASE/npm/bin last: the image's claude wins when there is one.
export JAVA_HOME=$BASE/jdk PATH=$HOME/.local/bin:$BASE/maven/bin:$BASE/jdk/bin:$PATH:$BASE/npm/bin
say "Claude Code $(claude --version 2>/dev/null) ($(command -v claude))"

# The agent's own Nevia identity (editor key, see agent.sh setup). Kept in a shell variable
# that children don't inherit; the key file (readable by this user only) exists only while this
# script needs nevia, not while Claude runs.
NEVIA_KEY=$AGENT_NEVIA_KEY
unset AGENT_NEVIA_KEY
KEY_FILE=$BASE/tmp/nevia-key
key_on() { (umask 077 && printf '%s\n' "$NEVIA_KEY" > "$KEY_FILE"); }
key_off() { rm -f "$KEY_FILE"; }
# A computer's stored env can't use the reserved NEVIA_* names, so the CLI's ones are set here.
export NEVIA_KEY_FILE=$KEY_FILE NEVIA_WORKSPACE=$AGENT_NEVIA_WORKSPACE
trap key_off EXIT
key_on
nv() { nevia --non-interactive "$@"; }
nv whoami >/dev/null 2>&1 || die "the agent's Nevia key is not valid: run ./agent.sh setup --rotate"

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
[ -x "$DEPLOY" ] || die "no executable $DEPLOY in $REPO ($BASE_BRANCH)"
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
step "2/6 launching the app on $BASE_COMPUTER + $BASE_DATABASE, 3/6 testing it"
if COMPUTER=$BASE_COMPUTER PG_DATABASE=$BASE_DATABASE logged "$BASE/log/base.log" "./$DEPLOY" check; then
  say "$BASE_COMPUTER already runs and passes its checks: not redeployed"
  BASE_STATE="already running"
else
  say "$BASE_COMPUTER is not running or fails its checks: deploying $BASE_BRANCH"
  COMPUTER=$BASE_COMPUTER PG_DATABASE=$BASE_DATABASE logged "$BASE/log/base.log" "./$DEPLOY" deploy ||
    die "$BASE_BRANCH does not pass its checks on $BASE_COMPUTER: no PR"
  BASE_STATE="\`$BASE_BRANCH\` deployed"
fi

# --- 4. Claude: evaluate the task, implement it (code only) -------------------------
step "4/6 Claude Code on the task: $(printf '%s' "$TASK" | head -c 100)"
PROMPT="You are working in a clone of $REPO. Your task:

$TASK

Context:
- The application is a Maven project ($POM).
- You work on the code only. After you, the pipeline deploys your change to a sandbox (a copy of
  the application and of its database) and checks GET /healthz and GET /; if that fails, no PR.

Steps:
1. Evaluate the task: is it clear, feasible and in the scope of this project? If not, change
   no file and explain why.
2. Implement the smallest change that solves it, in the style of the surrounding code.
3. Validate it with \`mvn -q -B -f $POM package\` (add or adapt tests when it makes sense).

Rules:
- Do not run git. Do not modify $DEPLOY or $APP_BOOTSTRAP.
- Your final reply is the pull request, in exactly this form and nothing else:
  <title>a short imperative title, under 72 characters, e.g. Reject non-GET requests on /</title>
  <body>GitHub markdown with the sections \"## Task evaluation\", \"## Solution\" and \"## Tests\"
  (the commands you ran and what they returned)</body>"

# With --output-format json, Claude Code reports its errors (e.g. an expired login) in the
# result on stdout, not on stderr.
claude_error() {
  node -e '
    try {
      const r = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
      console.log(`Claude Code: ${r.result || r.subtype}` + (r.api_error_status ? ` (API status ${r.api_error_status})` : ""));
    } catch {}
  ' "$BASE/tmp/result.json"
}

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
    --disallowedTools "Edit(./$DEPLOY),Write(./$DEPLOY),Edit(./$APP_BOOTSTRAP),Write(./$APP_BOOTSTRAP)" \
    ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} \
    > "$BASE/tmp/result.json" 2>> "$BASE/log/claude.log" ||
  { tail -20 "$BASE/log/claude.log" >&2; claude_error >&2; die "Claude Code failed: no PR"; }
key_on

node -e '
  const r = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
  if (r.is_error) {
    console.error(`Claude Code: ${r.result || r.subtype}` + (r.api_error_status ? ` (API status ${r.api_error_status})` : ""));
    process.exit(1);
  }
  // pr.md = title on the first line, then the body. From the <title>/<body> tags the prompt asks
  // for; without them, the first line of the reply is the title.
  const text = (r.result || "").trim();
  const tag = t => (text.match(new RegExp(`<${t}>([\\s\\S]*?)</${t}>`)) || [])[1];
  let title = (tag("title") || "").trim(), body = (tag("body") || "").trim();
  if (!title) { const lines = text.split("\n"); title = lines.shift(); body = lines.join("\n").trim(); }
  title = title.replace(/^#+\s*/, "").replace(/\s+/g, " ").slice(0, 120);
  require("fs").writeFileSync(process.argv[2], `${title}\n\n${body}\n`);
' "$BASE/tmp/result.json" "$BASE/tmp/pr.md" || die "Claude Code returned an error: no PR"

# Edits to the deploy scripts are refused above; drop any that slipped through anyway.
for p in "$DEPLOY" "$APP_BOOTSTRAP"; do
  if git ls-files --error-unmatch "$p" >/dev/null 2>&1; then git checkout -q -- "$p"; git clean -qfd -- "$p"; fi
done
if [ -z "$(git status --porcelain)" ]; then
  say "no change proposed, no sandbox, no PR:"
  cat "$BASE/tmp/pr.md"
  exit 0
fi
say "build"
mvn -q -B -f "$POM" package >> "$BASE/log/build.log" 2>&1 ||
  { tail -40 "$BASE/log/build.log" >&2; die "build fails with the agent's change: no sandbox, no PR"; }

# --- 5. test the new code: checkpoint of the app, branch of the database -----------
step "5/6 testing the change: checkpoint $CHECKPOINT of $BASE_COMPUTER, branch $SANDBOX_DATABASE of $BASE_DATABASE"
nv checkpoint create -c "$BASE_COMPUTER" --name "$CHECKPOINT" --wait >/dev/null
nv pg branch "$BASE_DATABASE" -b "$SANDBOX_DATABASE" --wait >/dev/null
nv restore "$CHECKPOINT" -c "$BASE_COMPUTER" --name "$SANDBOX_COMPUTER" --wait >/dev/null

# Right after a restore, the sandbox's network (DNS) may not answer yet; *.live.nevia.cloud
# is a wildcard, so a lookup that succeeds means the network is up. Wait until the sandbox
# resolves the branch host several times in a row (the app also retries at startup).
host=$(nv --json pg status "$SANDBOX_DATABASE" | jq -r '.database.host // empty')
[ -n "$host" ] || die "no host for $SANDBOX_DATABASE (cleanup: ./agent.sh cleanup $RUN)"
say "waiting for $SANDBOX_COMPUTER to resolve $host"
i=0 ok=0
while [ $ok -lt 5 ]; do
  if nevia exec -n "$SANDBOX_COMPUTER" -- getent hosts "$host" >/dev/null 2>&1; then ok=$((ok + 1)); else ok=0; fi
  i=$((i + 1))
  [ $i -lt 120 ] || die "$SANDBOX_COMPUTER still does not resolve $host after ~5 min (cleanup: ./agent.sh cleanup $RUN)"
  sleep 2
done

# The app's deploy.sh with the sandbox names: stores the branch URI in its own secret, points the
# restored computer at it, pushes the working tree (the new code), restarts the app and checks it.
say "deploying the change on $SANDBOX_COMPUTER + $SANDBOX_DATABASE"
COMPUTER=$SANDBOX_COMPUTER PG_DATABASE=$SANDBOX_DATABASE SECRET_DATABASE_URL=$SANDBOX_SECRET \
  logged "$BASE/log/validation.log" "./$DEPLOY" deploy ||
  die "the change fails its checks on $SANDBOX_COMPUTER: no PR (sandbox kept: ./agent.sh cleanup $RUN)"
SANDBOX_URL=$(nv --json status "$SANDBOX_COMPUTER" | jq -r 'first(.. | objects | select(has("url")) | .url) // empty')

# --- 6. push the PR -------------------------------------------------------------------
step "6/6 opening the pull request"
TITLE=$(head -n 1 "$BASE/tmp/pr.md" | sed 's/^#* *//')
[ -n "$TITLE" ] || TITLE="Agent proposal"
BRANCH="agent/$RUN"
{
  printf '\n## Automated validation\n\n'
  printf -- '- `%s` + `%s` checked before the change (%s).\n' "$BASE_COMPUTER" "$BASE_DATABASE" "$BASE_STATE"
  printf -- '- With this change, `mvn package` passes.\n'
  printf -- '- Sandbox: computer `%s` restored from checkpoint `%s`, database `%s` branched from `%s`.\n' \
    "$SANDBOX_COMPUTER" "$CHECKPOINT" "$SANDBOX_DATABASE" "$BASE_DATABASE"
  printf -- '- `%s deploy` of this change on the sandbox returns:\n\n```\n' "$DEPLOY"
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

payload=$(TITLE=$TITLE BRANCH=$BRANCH BASE_BRANCH=$BASE_BRANCH node -e '
  const fs = require("fs"), e = process.env;
  const body = fs.readFileSync(process.argv[1], "utf8").split("\n").slice(1).join("\n").trim();
  console.log(JSON.stringify({ title: e.TITLE, head: e.BRANCH, base: e.BASE_BRANCH, body }));
' "$BASE/tmp/pr.md")
url=$(curl -fsS -X POST "https://api.github.com/repos/$REPO/pulls" \
        -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
        -d "$payload" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s).html_url))') ||
  die "branch $BRANCH pushed but the pull request could not be created"

# The sandbox stays up as the PR's live preview, until ./agent.sh cleanup.
say "preview: $SANDBOX_URL (sandbox $SANDBOX_COMPUTER + $SANDBOX_DATABASE, running)"
say "pull request: $url"
