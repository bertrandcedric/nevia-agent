#!/bin/sh
# The deploy layer: the app of APP_DIR (default: the current directory) on Nevia, with the
# database its application.properties names (database.kind: mongo or postgres), then checks
# GET /healthz and GET /.
#
# Usage: deploy.sh deploy             create or wake the database and the app, deploy, check
#        deploy.sh recreate           delete the app and MongoDB computers (data included), then deploy
#        deploy.sh destroy [--yes]    delete the stack: computers, databases (all data), secrets
#
# COMPUTER, MONGO_COMPUTER and PG_DATABASE name the stack. SANDBOX=<run> gives the app on COMPUTER
# a database of its own, <name>-<run>: a branch of the stack's when it has the same kind
# (STACK_DB_KIND), a new one otherwise. A new database gets the app's seed data:
# local/mongo-init/*.js or local/postgres-init/*.sql. APP_JAR=<jar> runs that jar instead of
# building the sources on the computer.
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
TAG=deploy
APP_DIR=$(cd "${APP_DIR:-$PWD}" && pwd)

case ${1:-} in
  deploy|recreate|destroy) ;;
  *) sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0" >&2; exit 1 ;;
esac

# agent/.env when run from uc2/deploy on your machine (none on the agent computer).
load_env "$DIR/../../.env" UC2
COMPUTER=${COMPUTER:-hello-app}
MONGO_COMPUTER=${MONGO_COMPUTER:-hello-mongo}
PG_DATABASE=${PG_DATABASE:-hello-pg}
SECRET_MONGO_PW=${SECRET_MONGO_PW:-HELLO_MONGO_ROOT_PASSWORD}
SECRET_CHISEL_PW=${SECRET_CHISEL_PW:-HELLO_CHISEL_PASSWORD}
SECRET_DATABASE_URL=${SECRET_DATABASE_URL:-HELLO_DATABASE_URL}
MONGO_USER=admin
CHISEL_USER=tunnel

for c in nevia jq openssl; do command -v $c >/dev/null || die "$c not found"; done

if [ "$1" != destroy ]; then
  PROPS=$APP_DIR/src/main/resources/application.properties
  [ -f "$APP_DIR/pom.xml" ] && [ -f "$PROPS" ] || die "no Maven app in $APP_DIR (pom.xml, $PROPS)"
  DB_KIND=$(sed -n 's/^[[:space:]]*database\.kind[[:space:]]*[=:][[:space:]]*//p' "$PROPS" | tail -1 | tr -d '[:space:]')
  case $DB_KIND in mongo|postgres) ;; *) die "database.kind must be mongo or postgres, got '$DB_KIND'" ;; esac
fi

SANDBOX=${SANDBOX:-}
if [ -n "$SANDBOX" ]; then
  case $SANDBOX in *[!0-9]*) die "SANDBOX is a run id, got '$SANDBOX'" ;; esac
  BASE_MONGO=$MONGO_COMPUTER BASE_PG=$PG_DATABASE
  MONGO_COMPUTER=$MONGO_COMPUTER-$SANDBOX PG_DATABASE=$PG_DATABASE-$SANDBOX
  SECRET_DATABASE_URL=${SECRET_DATABASE_URL}_$SANDBOX
fi

# new_secret <name>: a random password straight into a workspace secret.
new_secret() { openssl rand -hex 24 | nv --yes secrets "$1" --file - >/dev/null; }

# wake [pg] <name> <json>: wake a sleeping computer or database.
wake() {
  if [ "$1" = pg ]; then shift; set -- "pg awake" "$@"; else set -- awake "$@"; fi
  [ "$(json_field "$3" state)" != sleeping ] || nv $1 "$2" --wait >/dev/null
}

mongo_up() {
  json=$(status_json "$MONGO_COMPUTER")
  if [ -z "$json" ] && [ -n "$SANDBOX" ]; then
    # A branch keeps the data and the env (same passwords).
    [ -n "$(status_json "$BASE_MONGO")" ] || die "no computer $BASE_MONGO to branch for the sandbox"
    say "branching $BASE_MONGO into $MONGO_COMPUTER"
    nv branch "$MONGO_COMPUTER" -s "$BASE_MONGO" --wait >/dev/null
  elif [ -z "$json" ]; then
    say "creating computer $MONGO_COMPUTER"
    new_secret "$SECRET_MONGO_PW"
    new_secret "$SECRET_CHISEL_PW"
    nv new "$MONGO_COMPUTER" -p 443:8080 \
      -e "MONGO_ROOT_USERNAME=$MONGO_USER" -e "MONGO_ROOT_PASSWORD={{secrets.$SECRET_MONGO_PW}}" \
      -e "CHISEL_USER=$CHISEL_USER" -e "CHISEL_PASSWORD={{secrets.$SECRET_CHISEL_PW}}" --wait >/dev/null
  else
    wake "$MONGO_COMPUTER" "$json"
  fi
  if ls "$APP_DIR"/local/mongo-init/*.js >/dev/null 2>&1; then
    push_dir "$MONGO_COMPUTER" hello-mongo/init "$APP_DIR/local/mongo-init" .
  fi
  cat "$DIR/lib.sh" "$DIR/mongo/bootstrap.sh" | nevia exec "$MONGO_COMPUTER" -- sh -s

  url=$(json_field "$(status_json "$MONGO_COMPUTER")" url)
  [ -n "$url" ] || die "computer $MONGO_COMPUTER has no url"
  APP_ENV="MONGO_TUNNEL_URL=$url
CHISEL_USER=$CHISEL_USER
CHISEL_PASSWORD={{secrets.$SECRET_CHISEL_PW}}
MONGODB_URI=mongodb://$MONGO_USER:{{secrets.$SECRET_MONGO_PW}}@127.0.0.1:27017/hello?authSource=admin&directConnection=true"
  OTHER_ENV="DATABASE_URL PG_CA_FILE"
}

pg_up() {
  new=
  json=$(status_json pg "$PG_DATABASE")
  if [ -z "$json" ] && [ -n "$SANDBOX" ] && [ "${STACK_DB_KIND:-postgres}" = postgres ] &&
     [ -n "$(status_json pg "$BASE_PG")" ]; then
    say "branching $BASE_PG into $PG_DATABASE"
    nv pg branch "$BASE_PG" -b "$PG_DATABASE" --wait >/dev/null
  elif [ -z "$json" ]; then
    say "creating Postgres database $PG_DATABASE"
    nv pg new "$PG_DATABASE" --wait >/dev/null
    new=1
  else
    wake pg "$PG_DATABASE" "$json"
  fi
  # The URI goes straight into a workspace secret; captured first, so a failure stops here.
  uri=$(nv pg connect "$PG_DATABASE" --uri)
  [ -n "$uri" ] || die "no URI for $PG_DATABASE"
  printf '%s\n' "$uri" | nv --yes secrets "$SECRET_DATABASE_URL" --file - >/dev/null
  nv pg connect "$PG_DATABASE" --ca "$APP_DIR/nevia-pg-ca.pem" >/dev/null
  # The seed data, only into a database created here (a new Nevia database is not empty).
  if [ -n "$new" ] && ls "$APP_DIR"/local/postgres-init/*.sql >/dev/null 2>&1; then
    command -v psql >/dev/null || die "psql not found: it loads local/postgres-init"
    for f in "$APP_DIR"/local/postgres-init/*.sql; do
      say "loading local/postgres-init/$(basename "$f") into $PG_DATABASE"
      PGSSLROOTCERT=$APP_DIR/nevia-pg-ca.pem psql "$uri" -X -q -v ON_ERROR_STOP=1 --single-transaction -f "$f" >/dev/null ||
        die "local/postgres-init/$(basename "$f") failed"
    done
  fi
  unset uri
  APP_ENV="DATABASE_URL={{secrets.$SECRET_DATABASE_URL}}
PG_CA_FILE=nevia-pg-ca.pem"
  OTHER_ENV="MONGODB_URI MONGO_TUNNEL_URL CHISEL_USER CHISEL_PASSWORD"
}

# app_up: the app's computer with the database's env, the sources, the app started.
app_up() {
  set --
  while IFS= read -r kv; do set -- "$@" -e "$kv"; done <<EOF
$APP_ENV
EOF
  json=$(status_json "$COMPUTER")
  if [ -z "$json" ]; then
    say "creating computer $COMPUTER"
    nv new "$COMPUTER" -p 443:8080 "$@" --wait >/dev/null
  else
    wake "$COMPUTER" "$json"
    nv set "$COMPUTER" "$@" >/dev/null
    # After a change of database kind, drop the other kind's env.
    for k in $OTHER_ENV; do
      if printf '%s' "$json" | jq -e --arg k "$k" '.computer.env_keys | index($k)' >/dev/null; then
        nv set "$COMPUTER" --unset-env "$k" >/dev/null
      fi
    done
  fi
  if [ -n "${APP_JAR:-}" ]; then
    tmp=$(mktemp -d)
    cp "$APP_JAR" "$tmp/app.jar"
    [ "$DB_KIND" != postgres ] || cp "$APP_DIR/nevia-pg-ca.pem" "$tmp/"
    push_dir "$COMPUTER" app/src "$tmp" .
    rm -rf "$tmp"
  else
    set -- pom.xml src
    [ "$DB_KIND" != postgres ] || set -- "$@" nevia-pg-ca.pem
    push_dir "$COMPUTER" app/src "$APP_DIR" "$@"
  fi
  cat "$DIR/lib.sh" "$DIR/app/bootstrap.sh" | nevia exec "$COMPUTER" -e "DB_KIND=$DB_KIND" -- sh -s
}

check() {
  url=$(json_field "$(status_json "$COMPUTER")" url)
  [ -n "$url" ] || die "computer $COMPUTER has no url"
  say "checking $url"
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 "$url/healthz" || die "$url/healthz failed"
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 "$url/" || die "$url/ failed"
}

# rm_computers <name>...: delete those that exist.
rm_computers() {
  for c in "$@"; do
    [ -n "$(status_json "$c")" ] || continue
    say "deleting computer $c"
    nv --yes rm "$c" --wait >/dev/null || die "could not delete $c (a sandbox restored from it may be live: agent.sh cleanup)"
  done
}

destroy() {
  if [ "${1:-}" != --yes ]; then
    [ -t 0 ] || die "destroy needs confirmation: $0 destroy --yes"
    printf 'Delete %s, %s and %s (all data) and their secrets? Type "destroy": ' "$COMPUTER" "$MONGO_COMPUTER" "$PG_DATABASE"
    read -r answer; [ "$answer" = destroy ] || die "aborted"
  fi
  rm_computers "$COMPUTER" "$MONGO_COMPUTER"
  if [ -n "$(status_json pg "$PG_DATABASE")" ]; then
    say "deleting Postgres database $PG_DATABASE"; nv --yes pg rm "$PG_DATABASE" --wait >/dev/null
  fi
  # A sandbox's MongoDB branch uses the stack's secrets: leave them.
  set -- "$SECRET_DATABASE_URL"
  [ -n "$SANDBOX" ] || set -- "$@" "$SECRET_MONGO_PW" "$SECRET_CHISEL_PW"
  for s in "$@"; do
    rc=0; nv --yes secrets unset "$s" >/dev/null 2>&1 || rc=$?
    case $rc in 0) say "deleted secret $s" ;; 3) ;; *) die "nevia secrets unset $s failed (exit $rc)" ;; esac
  done
  rm -f "$APP_DIR/nevia-pg-ca.pem"
}

case $1 in
  destroy) destroy "${2:-}" ;;
  *)
    if [ "$1" = recreate ]; then
      [ -z "$SANDBOX" ] || die "recreate is for the stack, not a sandbox"
      rm_computers "$COMPUTER" "$MONGO_COMPUTER"
    fi
    step "1/3 database: $DB_KIND${SANDBOX:+ (sandbox $SANDBOX)}"
    if [ "$DB_KIND" = mongo ]; then mongo_up; else pg_up; fi
    step "2/3 app computer $COMPUTER"
    app_up
    step "3/3 checking the app"
    check ;;
esac
