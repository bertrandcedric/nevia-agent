# The computer as every step uses it: sourced by seed/seed.sh, analyse/analyse.sh and
# migrate/run.sh (pushed to ~/uc3/scripts/<step>/ by lab.sh, next to lib.sh). Definitions only,
# and the tools, installed once: MongoDB, mongo2pg, mongosh, srcmongo, nvpg, the node packages, psql.
# The source is the MongoDB of this computer (127.0.0.1:27018, database hello), which seed fills.
# Env: AGENT_NEVIA_KEY and AGENT_NEVIA_KEY_EXPIRES (the computer's Nevia key, one for uc1, uc2, uc3:
# agent/common/computer.sh), LAB_NEVIA_WORKSPACE, PG_NAME (the target), and
# optionally CLAUDE_MODEL, CLAUDE_MAX_TURNS, ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN, or a
# gateway's ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL, ANTHROPIC_DEFAULT_*_MODEL
# (Claude Code reads them itself).
set -eu
TAG=uc3
SCRIPTS=$(cd "$(dirname "$0")/.." && pwd)
. "$SCRIPTS/lib.sh"
LAB=$HOME/uc3
BIN=$LAB/bin
mkdir -p "$BIN" "$LAB/tmp" "$LAB/runs" "$LAB/work"

M2P_REPO=${M2P_REPO:-pmpetit/mongo2pg}
M2P_TAG=${M2P_TAG:-mongo2pg-v0.8.0}
MONGOSH_TAG=${MONGOSH_TAG:-v2.13.0}
PG_NAME=${PG_NAME:-uc3-m2p}
# CLAUDE_MODEL in .env, else a gateway's ANTHROPIC_MODEL (its own name for the model), else Opus
# (Haiku gave up early; claude-sonnet-5-5 does the job for much less).
CLAUDE_MODEL=${CLAUDE_MODEL:-${ANTHROPIC_MODEL:-claude-opus-5-5}}
MONGO_VERSION=${MONGO_VERSION:-8.0.34}
MONGO_DB=hello
MONGO_URI=mongodb://127.0.0.1:27018/?directConnection=true
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
  [ -n "${AGENT_NEVIA_KEY:-}" ] || die "no AGENT_NEVIA_KEY on this computer: ./lab.sh setup"
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
  printf 'NEVIA_WORKSPACE=%s\nPG_NAME=%s\nCONFIG=%s\nSCHEMA=%s\nWORK=%s\nATTEMPTS=%s\n' \
    "$NEVIA_WORKSPACE" "$PG_NAME" "$CONFIG" "$NAME" "$WORK" "$LAB/attempts" > "$LAB/lab.env"
}

# key_check: the key's expiry (AGENT_NEVIA_KEY_EXPIRES, set with it): a warning 3 days before, an
# error after; then whether Nevia takes it (a rotation revokes the one before).
key_check() {
  case ${AGENT_NEVIA_KEY_EXPIRES:-} in
    ''|*[!0-9]*) ;;
    *) left=$((AGENT_NEVIA_KEY_EXPIRES - $(date +%s)))
       [ "$left" -gt 0 ] || die "the agent's Nevia key has expired: ./lab.sh rotate-key (on your machine)"
       [ "$left" -gt 259200 ] || say "warning: the agent's Nevia key expires in $((left / 3600)) h: ./lab.sh rotate-key (on your machine), once no run is going" ;;
  esac
  nv whoami >/dev/null 2>&1 || die "the agent's Nevia key is refused (expired, or rotated since this computer got it?): ./lab.sh rotate-key (on your machine)"
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
# node packages of context.mjs and of the migrate tools (in ~/uc3/node_modules, found from
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

# mongo_up: MongoDB (the release binary, installed once) started on 127.0.0.1:27018 unless it runs,
# then a wait for it to answer (over HTTP, mongod says so). A computer that slept has lost it.
mongo_up() {
  if [ ! -x "$BIN/mongod" ]; then
    [ "$(uname -m)" = x86_64 ] || die "no MongoDB build here for $(uname -m)"
    # The debian12 build runs on the Nevia image (Debian 13).
    url="https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-debian12-$MONGO_VERSION.tgz"
    say "downloading $url"
    fetch "$url" "$LAB/tmp/mongodb.tgz"
    echo "$(curl -fsSL "$url.sha256" | cut -d' ' -f1)  $LAB/tmp/mongodb.tgz" | sha256sum -c - >/dev/null ||
      die "MongoDB checksum mismatch"
    rm -rf "$LAB/tmp/mongodb" && mkdir -p "$LAB/tmp/mongodb"
    tar -xzf "$LAB/tmp/mongodb.tgz" -C "$LAB/tmp/mongodb" && cp "$LAB"/tmp/mongodb/*/bin/mongod "$BIN/"
    rm -rf "$LAB/tmp/mongodb" "$LAB/tmp/mongodb.tgz"
  fi
  mkdir -p "$LAB/mongo/data"
  if ! running "$LAB/mongo/mongod.pid"; then
    say "starting MongoDB $MONGO_VERSION on 127.0.0.1:27018"
    "$BIN/mongod" --dbpath "$LAB/mongo/data" --bind_ip 127.0.0.1 --port 27018 --wiredTigerCacheSizeGB 0.25 \
      --fork --logpath "$LAB/mongo/mongod.log" --logappend --pidfilepath "$LAB/mongo/mongod.pid" >/dev/null ||
      { tail -30 "$LAB/mongo/mongod.log" >&2; die "mongod failed to start"; }
  fi
  i=0
  until curl -sS --max-time 2 http://127.0.0.1:27018/ 2>/dev/null | grep -q MongoDB; do
    i=$((i + 1)); [ $i -lt 30 ] || { tail -30 "$LAB/mongo/mongod.log" >&2; die "mongod not answering after 30 s"; }
    sleep 1
  done
}

# counts: the documents of every collection of the source, as JSON ({"<collection>": n}).
counts() {
  srcmongo --eval 'print(JSON.stringify(Object.fromEntries(db.getCollectionNames().sort().map(c => [c, db.getCollection(c).countDocuments()]))))'
}

# The total time of the command, also when it fails (STEP_CMD: what it is, set by the script).
# While this script runs, it is in the computer's register of runs (~/.busy, any use case: see
# agent/common/computer.sh): nothing puts the computer to sleep or rotates the key under it. At the
# end it leaves it, and deletes its key file if it wrote one.
mkdir -p "$HOME/.busy" && BUSY=$HOME/.busy/uc3-$$ && echo $$ > "$BUSY"
trap 'rc=$?; rm -f "$BUSY" ${KEY_FILE:+"$KEY_FILE"}; [ -z "${STEP_CMD:-}" ] || done_ "$STEP_CMD on the computer"; exit $rc' EXIT
