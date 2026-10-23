#!/bin/sh
# Deploy an application on Nevia: create (or wake) the database and the app computer, push the
# sources, start the app, then check it answers over HTTPS (GET /healthz checks the database, GET / hits it).
#
# Usage: <path>/deploy.sh deploy            create or wake the database and the app, deploy, check
#        <path>/deploy.sh recreate          delete the app and MongoDB computers (data included), then deploy
#        <path>/deploy.sh check             check only
#        <path>/deploy.sh down              sleep the app and its database
#        <path>/deploy.sh destroy [--yes]   delete the whole stack (asks first; --yes skips)
#        <path>/deploy.sh help              this help
#
# The application is APP_DIR (the current directory by default): a Maven project (pom.xml, src)
# whose src/main/resources/application.properties sets database.kind, the database it wants.
# `mongo` is a MongoDB computer reached through a chisel tunnel (see mongo/bootstrap.sh),
# `postgres` a Nevia Postgres database. Only that one is deployed; the application carries none
# of this.
#
# COMPUTER, MONGO_COMPUTER and PG_DATABASE name the stack. SANDBOX=<run> deploys an agent
# sandbox instead: the app on COMPUTER (restored by the agent), and a database of its own,
# <name>-<run>: a branch of the stack's database when it has the same kind, a new one otherwise.
# A new Postgres gets the app's local/postgres-init/*.sql (psql), a new MongoDB its
# local/mongo-init/*.js, before the app starts.
set -eu

# This script and the bootstraps it sends to the computers.
DIR=$(cd "$(dirname "$0")" && pwd)
# The application to deploy: its pom.xml and src, and where the Postgres CA is written.
APP_DIR=$(cd "${APP_DIR:-$PWD}" && pwd)

# usage: the header comment of this file, up to the first line of code.
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
# A command is required: without one (or with -h/--help/help), print the help.
case ${1:-} in
  '')             usage; exit 1 ;;
  -h|--help|help) usage; exit 0 ;;
  deploy|recreate|check|down|destroy) ;;
  *)              printf 'error: unknown command "%s"\n\n' "$1" >&2; usage >&2; exit 1 ;;
esac

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
step() { printf '\n\033[1m─── %s ───\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# load_env <file>: export KEY=VALUE lines not already set in the shell (values never echoed).
load_env() {
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    key=${line%%=*}; val=${line#*=}
    case $key in ''|*[!A-Za-z0-9_]*) die "$1: invalid variable name '$key'" ;; esac
    case $val in \"*\") val=${val#\"}; val=${val%\"} ;; \'*\') val=${val#\'}; val=${val%\'} ;; esac
    eval "[ -n \"\${$key+x}\" ]" || export "$key=$val"
  done < "$1"
}
# This layer's own .env, then the agent's next to it: load_env only sets what is unset, so the
# first wins, and the shell wins over both. On the agent computer the env is already there.
load_env "$DIR/.env"
load_env "$DIR/../.env"

COMPUTER=${COMPUTER:-hello-app}
MONGO_COMPUTER=${MONGO_COMPUTER:-hello-mongo}
PG_DATABASE=${PG_DATABASE:-hello-pg}
# Workspace secrets referenced by the computers' env; deploy.sh writes them, never stores them.
SECRET_MONGO_PW=${SECRET_MONGO_PW:-HELLO_MONGO_ROOT_PASSWORD}
SECRET_CHISEL_PW=${SECRET_CHISEL_PW:-HELLO_CHISEL_PASSWORD}
SECRET_DATABASE_URL=${SECRET_DATABASE_URL:-HELLO_DATABASE_URL}
MONGO_USER=admin
CHISEL_USER=tunnel

# Only deploy and recreate need the application; check, down and destroy work on the names alone.
if [ "$1" = deploy ] || [ "$1" = recreate ]; then
  [ -f "$APP_DIR/pom.xml" ] || die "no pom.xml in $APP_DIR (set APP_DIR, or run this from the application)"
  APP_PROPERTIES=src/main/resources/application.properties
  [ -f "$APP_DIR/$APP_PROPERTIES" ] || die "no $APP_PROPERTIES in $APP_DIR"
  # database.kind is the one key read here; the rest of that file is the application's business.
  DB_KIND=$(sed -n 's/^[[:space:]]*database\.kind[[:space:]]*[=:][[:space:]]*//p' "$APP_DIR/$APP_PROPERTIES" |
    tail -1 | tr -d '[:space:]')
  case $DB_KIND in
    mongo|postgres) ;;
    *) die "$APP_PROPERTIES: database.kind must be mongo or postgres, got '$DB_KIND'" ;;
  esac
fi

# A sandbox gets its own database, named after the stack's one. A MongoDB branch keeps the
# source's env, so the same secrets; a Postgres sandbox stores its URI in a secret of its own.
SANDBOX=${SANDBOX:-}
if [ -n "$SANDBOX" ]; then
  case $SANDBOX in *[!0-9]*) die "SANDBOX is a run id, digits only, got '$SANDBOX'" ;; esac
  BASE_MONGO=$MONGO_COMPUTER BASE_PG=$PG_DATABASE
  MONGO_COMPUTER=$MONGO_COMPUTER-$SANDBOX PG_DATABASE=$PG_DATABASE-$SANDBOX
  SECRET_DATABASE_URL=${SECRET_DATABASE_URL}_$SANDBOX
fi

command -v nevia >/dev/null || die "nevia CLI not found: curl -fsSL https://get.nevia.cloud | sh"
command -v jq >/dev/null || die "jq not found: brew install jq"
command -v openssl >/dev/null || die "openssl not found"

# nv: the Nevia CLI, non-interactive. Retried when Nevia rate-limits (exit 6: at most 30 token
# requests a minute): the output of a call is kept until it succeeds, its stdin (--file -) replayed.
nv() {
  nv_in=; case " $* " in *" - "*) nv_in=$(cat) ;; esac
  nv_try=1
  while :; do
    nv_rc=0
    nv_out=$(printf '%s' "$nv_in" | nevia --non-interactive "$@") || nv_rc=$?
    if [ "$nv_rc" -eq 6 ] && [ "$nv_try" -lt 5 ]; then
      echo "[nevia] rate limited: retrying in 60 s ($nv_try/4)" >&2
      nv_try=$((nv_try + 1)); sleep 60; continue
    fi
    [ -z "$nv_out" ] || printf '%s\n' "$nv_out"
    return "$nv_rc"
  done
}

# status_json <computer|pg> <name>: the resource as JSON, nothing if it doesn't exist (exit 3).
status_json() {
  if [ "$1" = pg ]; then set -- pg status "$2"; else set -- status "$2"; fi
  rc=0; out=$(nv --json "$@") || rc=$?
  case $rc in
    0) printf '%s' "$out" ;;
    3) ;;
    # With --json the CLI prints its error on stdout: show it.
    *) printf '%s\n' "$out" >&2; die "nevia $* failed (exit $rc)" ;;
  esac
}
json_field() { printf '%s' "$1" | jq -r --arg k "$2" 'first(.. | objects | select(has($k)) | .[$k]) // empty'; }

# new_secret <name>: a random password straight into a write-only workspace secret, never to disk.
new_secret() { openssl rand -hex 24 | nv --yes secrets "$1" --file - >/dev/null; }

mongo_up() {
  json=$(status_json computer "$MONGO_COMPUTER")
  if [ -z "$json" ] && [ -n "$SANDBOX" ]; then
    # The branch copies the data and keeps the env (same passwords); its processes aren't
    # copied, mongo/bootstrap.sh below starts them.
    [ -n "$(status_json computer "$BASE_MONGO")" ] ||
      die "no computer $BASE_MONGO to branch for the sandbox (a new MongoDB in a sandbox isn't supported)"
    log "branching computer $BASE_MONGO into $MONGO_COMPUTER"
    nv branch "$MONGO_COMPUTER" -s "$BASE_MONGO" --wait >/dev/null
  elif [ -z "$json" ]; then
    # A new computer means an empty MongoDB, so fresh passwords: its root user is created with them.
    log "creating computer $MONGO_COMPUTER (https 443 -> chisel :8080 -> mongod 127.0.0.1:27017)"
    new_secret "$SECRET_MONGO_PW"
    new_secret "$SECRET_CHISEL_PW"
    nv new "$MONGO_COMPUTER" -p 443:8080 \
      -e "MONGO_ROOT_USERNAME=$MONGO_USER" -e "MONGO_ROOT_PASSWORD={{secrets.$SECRET_MONGO_PW}}" \
      -e "CHISEL_USER=$CHISEL_USER" -e "CHISEL_PASSWORD={{secrets.$SECRET_CHISEL_PW}}" \
      --wait >/dev/null
  else
    state=$(json_field "$json" state)
    log "computer $MONGO_COMPUTER is $state"
    if [ "$state" = sleeping ]; then nv awake "$MONGO_COMPUTER" --wait >/dev/null; fi
  fi
  # The app's seed data, if it has some (local/mongo-init/*.js, as for its local docker-compose):
  # mongo/bootstrap.sh loads it while the database is still empty.
  if ls "$APP_DIR"/local/mongo-init/*.js >/dev/null 2>&1; then
    log "pushing local/mongo-init to $MONGO_COMPUTER"
    if tar --version 2>/dev/null | grep -q bsdtar; then tflags="--no-xattrs --no-mac-metadata"; else tflags=; fi
    COPYFILE_DISABLE=1 tar $tflags -czf - -C "$APP_DIR/local/mongo-init" . | base64 |
      nevia exec "$MONGO_COMPUTER" -- sh -c 'rm -rf ~/hello-mongo/init && mkdir -p ~/hello-mongo/init && base64 -d | tar -xzf - -C ~/hello-mongo/init'
  fi
  log "starting MongoDB and chisel on $MONGO_COMPUTER"
  nevia exec "$MONGO_COMPUTER" -- sh -s < "$DIR/mongo/bootstrap.sh"

  mongo_url=$(json_field "$(status_json computer "$MONGO_COMPUTER")" url)
  [ -n "$mongo_url" ] || die "computer $MONGO_COMPUTER has no url"
  # The app reaches mongod through its local chisel client on 127.0.0.1:27017.
  APP_ENV="MONGO_TUNNEL_URL=$mongo_url
CHISEL_USER=$CHISEL_USER
CHISEL_PASSWORD={{secrets.$SECRET_CHISEL_PW}}
MONGODB_URI=mongodb://$MONGO_USER:{{secrets.$SECRET_MONGO_PW}}@127.0.0.1:27017/hello?authSource=admin&directConnection=true"
  OTHER_ENV="DATABASE_URL PG_CA_FILE"
}

# pg_seed: the app's seed data, if it has some (local/postgres-init/*.sql, as for its local
# docker-compose), run by psql in the order of their names, each in one transaction, into a
# database with no table yet: a new one. A branch, or a database already in use, is left as is.
pg_seed() {
  ls "$APP_DIR"/local/postgres-init/*.sql >/dev/null 2>&1 || return 0
  command -v psql >/dev/null || die "psql not found: it loads local/postgres-init (install postgresql-client)"
  uri=$(nv pg connect "$PG_DATABASE" --uri)
  [ -n "$uri" ] || die "no URI for $PG_DATABASE"
  export PGSSLROOTCERT="$APP_DIR/nevia-pg-ca.pem"
  tables=$(psql "$uri" -X -A -t -q -c "select count(*) from information_schema.tables where table_schema = 'public'")
  if [ "$tables" = 0 ]; then
    for f in "$APP_DIR"/local/postgres-init/*.sql; do
      log "loading local/postgres-init/$(basename "$f") into $PG_DATABASE"
      psql "$uri" -X -q -v ON_ERROR_STOP=1 --single-transaction -f "$f" >/dev/null ||
        die "local/postgres-init/$(basename "$f") failed on $PG_DATABASE"
    done
  else
    log "$PG_DATABASE already has tables: local/postgres-init not loaded"
  fi
  unset uri PGSSLROOTCERT
}

pg_up() {
  json=$(status_json pg "$PG_DATABASE")
  # A sandbox branches the stack's Postgres only when the stack runs on it (STACK_DB_KIND, given by
  # the agent): a change that switches from MongoDB gets a new one, loaded with local/postgres-init.
  if [ -z "$json" ] && [ -n "$SANDBOX" ] && [ "${STACK_DB_KIND:-postgres}" = postgres ] &&
     [ -n "$(status_json pg "$BASE_PG")" ]; then
    log "branching Postgres $BASE_PG into $PG_DATABASE"
    nv pg branch "$BASE_PG" -b "$PG_DATABASE" --wait >/dev/null
  elif [ -z "$json" ]; then
    log "creating Postgres database $PG_DATABASE"
    nv pg new "$PG_DATABASE" --wait >/dev/null
  else
    state=$(json_field "$json" state)
    log "Postgres $PG_DATABASE is $state"
    if [ "$state" = sleeping ]; then nv pg awake "$PG_DATABASE" --wait >/dev/null; fi
  fi
  # The URI goes straight into a write-only workspace secret, never to disk. Captured first
  # so a failing `pg connect` stops here instead of storing an empty secret (no pipefail in sh).
  uri=$(nv pg connect "$PG_DATABASE" --uri)
  [ -n "$uri" ] || die "no URI for $PG_DATABASE"
  printf '%s\n' "$uri" | nv --yes secrets "$SECRET_DATABASE_URL" --file - >/dev/null
  unset uri
  nv pg connect "$PG_DATABASE" --ca "$APP_DIR/nevia-pg-ca.pem" >/dev/null
  pg_seed
  # PG_CA_FILE is relative to the sources dir on the computer, where the CA is unpacked.
  APP_ENV="DATABASE_URL={{secrets.$SECRET_DATABASE_URL}}
PG_CA_FILE=nevia-pg-ca.pem"
  OTHER_ENV="MONGODB_URI MONGO_TUNNEL_URL CHISEL_USER CHISEL_PASSWORD"
}

# computer_up: the app's computer with the env set by mongo_up or pg_up, then the sources.
computer_up() {
  set --
  while IFS= read -r kv; do set -- "$@" -e "$kv"; done <<EOF
$APP_ENV
EOF
  json=$(status_json computer "$COMPUTER")
  if [ -z "$json" ]; then
    log "creating computer $COMPUTER (https 443 -> app :8080)"
    nv new "$COMPUTER" -p 443:8080 "$@" --wait >/dev/null
  else
    state=$(json_field "$json" state)
    log "computer $COMPUTER is $state"
    if [ "$state" = sleeping ]; then nv awake "$COMPUTER" --wait >/dev/null; fi
    nv set "$COMPUTER" "$@" >/dev/null
    # After a change of database kind, drop the other kind's env.
    for k in $OTHER_ENV; do
      if printf '%s' "$json" | jq -e --arg k "$k" '.computer.env_keys | index($k)' >/dev/null; then
        nv set "$COMPUTER" --unset-env "$k" >/dev/null
      fi
    done
  fi

  log "pushing sources to $COMPUTER"
  set -- pom.xml src
  if [ "$DB_KIND" = postgres ]; then set -- "$@" nevia-pg-ca.pem; fi
  # macOS bsdtar would add xattrs/AppleDouble files; GNU tar (agent computer) has no such flags.
  if tar --version 2>/dev/null | grep -q bsdtar; then tflags="--no-xattrs --no-mac-metadata"; else tflags=; fi
  # shellcheck disable=SC2086
  COPYFILE_DISABLE=1 tar $tflags -czf - -C "$APP_DIR" "$@" | base64 |
    nevia exec "$COMPUTER" -- sh -c 'rm -rf ~/app/src && mkdir -p ~/app/src && base64 -d | tar -xzf - -C ~/app/src'

  log "starting the app on $COMPUTER ($DB_KIND)"
  nevia exec "$COMPUTER" -e "DB_KIND=$DB_KIND" -- sh -s < "$DIR/app/bootstrap.sh"
}

check() {
  url=$(json_field "$(status_json computer "$COMPUTER")" url)
  [ -n "$url" ] || die "computer $COMPUTER has no url"
  log "checking $url"
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 "$url/healthz" || die "$url/healthz failed"
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 "$url/" || die "$url/ failed"
  log "app OK: $url"
}

# down: sleep the app, then whichever database of the stack exists and runs.
down() {
  for c in "$COMPUTER" "$MONGO_COMPUTER"; do
    if [ "$(json_field "$(status_json computer "$c")" state)" = ready ]; then
      nv suspend "$c" --wait >/dev/null; log "slept $c"
    fi
  done
  if [ "$(json_field "$(status_json pg "$PG_DATABASE")" state)" = ready ]; then
    nv pg suspend "$PG_DATABASE" --wait >/dev/null; log "slept $PG_DATABASE"
  fi
}

# Irreversible: deletes the app, both kinds of database whichever exist (with their data), their
# secrets and the local CA. Idempotent: whatever is already gone is skipped.
destroy() {
  if [ "${1:-}" != --yes ]; then
    [ -t 0 ] || die "destroy needs confirmation: $0 destroy --yes"
    printf 'Delete computers %s and %s, database %s (all data) and their secrets? Type "destroy": ' \
      "$COMPUTER" "$MONGO_COMPUTER" "$PG_DATABASE"
    read -r answer
    [ "$answer" = destroy ] || die "aborted"
  fi

  for c in "$COMPUTER" "$MONGO_COMPUTER"; do
    if [ -n "$(status_json computer "$c")" ]; then
      log "deleting computer $c"
      nv --yes rm "$c" --wait >/dev/null
    else
      log "computer $c already gone"
    fi
  done
  if [ -n "$(status_json pg "$PG_DATABASE")" ]; then
    log "deleting Postgres database $PG_DATABASE"
    nv --yes pg rm "$PG_DATABASE" --wait >/dev/null
  else
    log "Postgres $PG_DATABASE already gone"
  fi

  # A sandbox's MongoDB branch uses the stack's secrets: leave them.
  set -- "$SECRET_DATABASE_URL"
  [ -n "$SANDBOX" ] || set -- "$@" "$SECRET_MONGO_PW" "$SECRET_CHISEL_PW"
  for s in "$@"; do
    rc=0; nv --yes secrets unset "$s" >/dev/null 2>&1 || rc=$?
    case $rc in
      0) log "deleted secret $s" ;;
      3) log "secret $s already gone" ;;
      *) die "nevia secrets unset $s failed (exit $rc)" ;;
    esac
  done
  rm -f "$APP_DIR/nevia-pg-ca.pem"
  log "stack destroyed"
}

# recreate: delete the app computer and the MongoDB computer (its data with it), so that deploy
# starts them from scratch: new computers, fresh install, seed data loaded again. The Postgres
# database and the secrets are kept (a new MongoDB computer gets new passwords anyway).
recreate() {
  [ -z "$SANDBOX" ] || die "recreate is for the stack, not a sandbox"
  for c in "$COMPUTER" "$MONGO_COMPUTER"; do
    [ -n "$(status_json computer "$c")" ] || continue
    log "deleting computer $c"
    nv --yes rm "$c" --wait >/dev/null ||
      die "could not delete $c (a sandbox restored from it may still be live: agent.sh ls, then cleanup)"
  done
}

case $1 in
  recreate|deploy)
    if [ "$1" = recreate ]; then step "deleting $COMPUTER and $MONGO_COMPUTER"; recreate; fi
    step "1/3 database: $DB_KIND${SANDBOX:+ (sandbox $SANDBOX)}"
    if [ "$DB_KIND" = mongo ]; then mongo_up; else pg_up; fi
    step "2/3 app computer $COMPUTER"
    computer_up
    step "3/3 checking the app"
    check ;;
  check)   step "checking the app"; check ;;
  down)    step "sleeping the stack"; down ;;
  destroy) step "deleting the stack"; destroy "${2:-}" ;;
esac
