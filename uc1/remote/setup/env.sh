# The computer as every step uses it: sourced by analyse/analyse.sh, migrate/run.sh and
# setup/status.sh (pushed to ~/m2p/scripts/<step>/ by rc-lab.sh, next to lib.sh, and the source's
# scripts in ~/m2p/scripts/source when there is one). Definitions only, and the tools, installed
# once: mongo2pg, mongosh, srcmongo, nvpg, the node packages, psql.
# Env: AGENT_NEVIA_KEY and AGENT_NEVIA_KEY_EXPIRES (the computer's Nevia key, one for uc1, uc2, uc3:
# agent/common/computer.sh), LAB_NEVIA_WORKSPACE, LAB_MONGO_URI (the MongoDB,
# as this computer reaches it), MONGO_DB (the database), PG_NAME (the target), and optionally
# CLAUDE_MODEL, CLAUDE_MAX_TURNS, ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN, or a gateway's
# ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL, ANTHROPIC_DEFAULT_*_MODEL (Claude Code reads
# them itself), and the source's.
set -eu
TAG=m2p
SCRIPTS=$(cd "$(dirname "$0")/.." && pwd)
. "$SCRIPTS/lib.sh"
LAB=$HOME/m2p
BIN=$LAB/bin
mkdir -p "$BIN" "$LAB/tmp" "$LAB/runs" "$LAB/work"

M2P_REPO=${M2P_REPO:-pmpetit/mongo2pg}
M2P_TAG=${M2P_TAG:-mongo2pg-v0.8.0}
MONGOSH_TAG=${MONGOSH_TAG:-v2.13.0}
PG_NAME=${PG_NAME:-rc-pg}
# CLAUDE_MODEL in .env, else a gateway's ANTHROPIC_MODEL (its own name for the model), else Opus
# (Haiku gave up early; claude-sonnet-5-5 does the job for much less).
CLAUDE_MODEL=${CLAUDE_MODEL:-${ANTHROPIC_MODEL:-claude-opus-5-5}}
MONGO_DB=${MONGO_DB:?missing env MONGO_DB}
MONGO_URI=${LAB_MONGO_URI:?missing env LAB_MONGO_URI}
# The project, its schema in Postgres: the database's name, as an identifier.
NAME=$(slug "$MONGO_DB")
PROJECT=$LAB/migration/$NAME
CONFIG=$PROJECT/config/$NAME.toml
WORK=$LAB/work
# The project as analyse left it, before any change of Claude's, and the documents of each
# collection then: each run starts from it, as long as the analyse step has not changed since
# (analyse_sum) and the source still holds the same documents.
PRISTINE=$LAB/migration/$NAME.pristine.tgz
COUNTS=$LAB/migration/$NAME.counts.json
analyse_sum() { cat "$SCRIPTS"/analyse/* | sha256sum | cut -d' ' -f1; }
# The source's scripts, if any (rc-lab.sh SOURCE).
SOURCE_SH=$SCRIPTS/source/source.sh
# nevia goes to ~/.local/bin (key_on), not on the PATH of nevia exec.
export PATH=$BIN:$HOME/.local/bin:$PATH
# NEVIA_* is reserved in a computer's env: the workspace comes as LAB_NEVIA_WORKSPACE.
export NEVIA_WORKSPACE=${LAB_NEVIA_WORKSPACE:?missing env LAB_NEVIA_WORKSPACE}

# The Nevia key, one for every use case on this computer (agent/common/computer.sh): in a file of
# this process only, deleted when it ends (another agent may be running, and none must find a key
# left behind), for nevia and nvpg (Claude's way to branch, which inherits NEVIA_KEY_FILE). The
# scripts this one starts use the same file. nevia is not on the image: the official installer
# (checksum checked) puts it in ~/.local/bin.
key_on() {
  [ -z "${NEVIA_KEY_FILE:-}" ] || return 0
  [ -n "${AGENT_NEVIA_KEY:-}" ] || die "no AGENT_NEVIA_KEY on this computer: ./rc-lab.sh setup"
  if ! command -v nevia >/dev/null; then
    say "installing the Nevia CLI"
    curl -fsSL https://get.nevia.cloud | sh >/dev/null || die "could not install the Nevia CLI"
  fi
  # The files of processes that are gone (killed before their trap ran).
  for f in "$LAB"/tmp/nevia-key.*; do [ -e "$f" ] && ! kill -0 "${f##*.}" 2>/dev/null && rm -f "$f"; done
  KEY_FILE=$LAB/tmp/nevia-key.$$
  (umask 077 && printf '%s\n' "$AGENT_NEVIA_KEY" > "$KEY_FILE")
  unset AGENT_NEVIA_KEY
  export NEVIA_KEY_FILE=$KEY_FILE
  key_check
  # What nvpg needs, none of it secret.
  printf 'NEVIA_WORKSPACE=%s\nPG_NAME=%s\nCONFIG=%s\n' "$NEVIA_WORKSPACE" "$PG_NAME" "$CONFIG" > "$LAB/lab.env"
}

# key_check: the key's expiry (AGENT_NEVIA_KEY_EXPIRES, set with it): a warning 3 days before, an
# error after; then whether Nevia takes it (a rotation revokes the one before).
key_check() {
  case ${AGENT_NEVIA_KEY_EXPIRES:-} in
    ''|*[!0-9]*) ;;
    *) left=$((AGENT_NEVIA_KEY_EXPIRES - $(date +%s)))
       [ "$left" -gt 0 ] || die "the agent's Nevia key has expired: agent/computer.sh rotate-key"
       [ "$left" -gt 259200 ] || say "warning: the agent's Nevia key expires in $((left / 3600)) h: agent/computer.sh rotate-key, once no run is going" ;;
  esac
  nv whoami >/dev/null 2>&1 || die "the agent's Nevia key is refused (expired, or rotated since this computer got it?): agent/computer.sh rotate-key"
}

# gh_asset <repo> <tag> <asset> <file>: a release asset, checked against the digest GitHub publishes.
gh_asset() {
  meta=$(curl -fsSL "https://api.github.com/repos/$1/releases/tags/$2")
  url=$(printf '%s' "$meta" | jq -r --arg n "$3" '.assets[] | select(.name == $n) | .browser_download_url')
  sum=$(printf '%s' "$meta" | jq -r --arg n "$3" '.assets[] | select(.name == $n) | .digest // empty' | sed 's/^sha256://')
  [ -n "$url" ] && [ -n "$sum" ] || die "no $3 with a digest in release $2 of $1"
  say "downloading $url"
  fetch "$url" "$4"
  echo "$sum  $4" | sha256sum -c - >/dev/null || die "$3: checksum mismatch"
}

# db_uri <uri> <db>: the URI with <db> as its database, its options kept. The database it named
# was also the one to authenticate against: it stays as authSource, unless the URI gives one.
db_uri() {
  rest=${1#*://} path=
  case $rest in */*) path=${rest#*/} ;; esac
  query=; case $path in *\?*) query=?${path#*\?} ;; esac
  auth=${path%%\?*}
  if [ -n "$auth" ] && [ "$auth" != "$2" ]; then
    case $query in *authSource=*) ;; '') query="?authSource=$auth" ;; *) query="$query&authSource=$auth" ;; esac
  fi
  printf '%s://%s/%s%s' "${1%%://*}" "${rest%%/*}" "$2" "$query"
}

# install: mongo2pg, used as it is (Claude improves the SQL mapping, not mongo2pg); mongosh with
# srcmongo, mongosh on the source database (its URI in the script, this user only); nvpg; the
# node packages of context.mjs and of the migrate tools (in ~/m2p/node_modules, found from
# scripts/ and work/); psql. Each once.
install() {
  case $(uname -m) in x86_64|amd64) m2p_arch=x86_64 sh_arch=x64 ;; aarch64|arm64) m2p_arch=aarch64 sh_arch=arm64 ;; *) die "no build for $(uname -m)" ;; esac
  if [ ! -x "$BIN/mongo2pg" ]; then
    gh_asset "$M2P_REPO" "$M2P_TAG" "mongo2pg-linux-$m2p_arch" "$LAB/tmp/mongo2pg"
    chmod +x "$LAB/tmp/mongo2pg" && mv "$LAB/tmp/mongo2pg" "$BIN/mongo2pg"
  fi
  if [ ! -x "$LAB/mongosh/mongosh" ]; then
    asset=mongosh-${MONGOSH_TAG#v}-linux-$sh_arch.tgz
    gh_asset mongodb-js/mongosh "$MONGOSH_TAG" "$asset" "$LAB/tmp/$asset"
    rm -rf "$LAB/mongosh" "$LAB/tmp/mongosh" && mkdir -p "$LAB/mongosh" "$LAB/tmp/mongosh"
    tar -xzf "$LAB/tmp/$asset" -C "$LAB/tmp/mongosh" && cp "$LAB"/tmp/mongosh/*/bin/* "$LAB/mongosh/"
    rm -rf "$LAB/tmp/mongosh" "$LAB/tmp/$asset"
  fi
  (umask 077 && printf '#!/bin/sh\n# mongosh on the source database %s (generated by setup/env.sh).\nexec %s --quiet "%s" "$@"\n' \
    "$MONGO_DB" "$LAB/mongosh/mongosh" "$(db_uri "$MONGO_URI" "$MONGO_DB")" > "$BIN/srcmongo") && chmod 700 "$BIN/srcmongo"
  cp "$SCRIPTS/migrate/nvpg" "$BIN/nvpg" && chmod +x "$BIN/nvpg"
  [ -d "$LAB/node_modules/yaml" ] && [ -d "$LAB/node_modules/pg" ] && [ -d "$LAB/node_modules/mongodb" ] ||
    npm install --prefix "$LAB" --no-audit --no-fund --silent yaml@2 pg@8 mongodb@7 >/dev/null ||
    die "could not install the node packages (yaml, pg, mongodb)"
  # psql is not on the image (a system package).
  if ! command -v psql >/dev/null; then
    say "installing the Postgres client"
    sudo -n apt-get -qq update >/dev/null && sudo -n env DEBIAN_FRONTEND=noninteractive \
      apt-get -qq install -y --no-install-recommends postgresql-client >/dev/null || die "could not install postgresql-client"
  fi
}

# counts: the documents of every collection of the source, as JSON ({"<collection>": n}).
counts() {
  srcmongo --eval 'print(JSON.stringify(Object.fromEntries(db.getCollectionNames().sort().map(c => [c, db.getCollection(c).countDocuments()]))))'
}

# The total time of the command, also when it fails (STEP_CMD: what it is, set by the script).
# While this script runs, it is in the computer's register of runs (~/.busy, any use case: see
# agent/common/computer.sh): nothing puts the computer to sleep or rotates the key under it. At the
# end it leaves it, and deletes its key file if it wrote one.
mkdir -p "$HOME/.busy" && BUSY=$HOME/.busy/uc1-$$ && echo $$ > "$BUSY"
trap 'rc=$?; rm -f "$BUSY" ${KEY_FILE:+"$KEY_FILE"}; [ -z "${STEP_CMD:-}" ] || done_ "$STEP_CMD on the computer"; exit $rc' EXIT
