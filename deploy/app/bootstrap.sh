#!/bin/sh
# Runs ON the Nevia app computer, piped through `nevia exec <computer> -- sh -s`, after deploy.sh
# has unpacked a classic Maven project (pom.xml, src) into ~/app/src. Idempotent:
#   - installs Temurin 21 and Maven (and chisel, for MongoDB) into $HOME on first run
#   - builds the project (mvn package, without the tests: they need the whole repository, which
#     isn't pushed here, and they ran where it is, e.g. on the agent) and takes the jar out of src/target
#   - MongoDB: (re)starts the chisel client (127.0.0.1:27017 -> MongoDB computer, see mongo/bootstrap.sh);
#     Postgres: stops it if it still runs
#   - (re)starts the app on :8080 and waits for it to answer on /healthz
#
# Env: DB_KIND (mongo or postgres, from deploy.sh's exec), then the computer's stored env:
# MONGODB_URI, MONGO_TUNNEL_URL, CHISEL_USER, CHISEL_PASSWORD for mongo; DATABASE_URL, PG_CA_FILE for postgres.
set -eu

JDK_MAJOR=21
MAVEN_VERSION=3.9.12
CHISEL_VERSION=1.10.1

BASE=$HOME/app
mkdir -p "$BASE/log" "$BASE/tmp"

say() { echo "[app] $*"; }
step() { printf '\n[app] ─── %s ───\n' "$*"; }
die() { echo "[app] error: $*" >&2; exit 1; }

case ${DB_KIND:-} in
  mongo)    required="MONGODB_URI MONGO_TUNNEL_URL CHISEL_USER CHISEL_PASSWORD" ;;
  postgres) required="DATABASE_URL PG_CA_FILE" ;;
  *)        die "DB_KIND must be mongo or postgres, got '${DB_KIND:-}'" ;;
esac
for v in $required; do
  eval "[ -n \"\${$v:-}\" ]" || die "missing env $v"
done
[ "$(uname -m)" = x86_64 ] || die "this script handles x86_64 only, got $(uname -m)"

fetch() { curl -fsSL "$1" -o "$2"; }

# --- install -----------------------------------------------------------------
step "1/3 tools (JDK 21, Maven${DB_KIND:+, chisel for $DB_KIND}), installed on first run only"
if [ ! -x "$BASE/jdk/bin/java" ]; then
  meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$JDK_MAJOR/hotspot?os=linux&architecture=x64&image_type=jdk")
  link=$(printf '%s' "$meta" | jq -r '.[0].binary.package.link')
  sum=$(printf '%s' "$meta" | jq -r '.[0].binary.package.checksum')
  say "downloading $link"
  fetch "$link" "$BASE/tmp/jdk.tgz"
  echo "$sum  $BASE/tmp/jdk.tgz" | sha256sum -c - >/dev/null || die "JDK checksum mismatch"
  mkdir -p "$BASE/jdk"
  tar -xzf "$BASE/tmp/jdk.tgz" -C "$BASE/jdk" --strip-components=1
  rm -rf "$BASE"/tmp/*
fi
export JAVA_HOME=$BASE/jdk PATH=$BASE/jdk/bin:$PATH

if [ ! -x "$BASE/maven/bin/mvn" ]; then
  url="https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$MAVEN_VERSION/apache-maven-$MAVEN_VERSION-bin.tar.gz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/maven.tgz"
  echo "$(curl -fsSL "$url.sha512" | cut -d' ' -f1)  $BASE/tmp/maven.tgz" | sha512sum -c - >/dev/null || die "Maven checksum mismatch"
  mkdir -p "$BASE/maven"
  tar -xzf "$BASE/tmp/maven.tgz" -C "$BASE/maven" --strip-components=1
  rm -rf "$BASE"/tmp/*
fi

if [ "$DB_KIND" = mongo ] && [ ! -x "$BASE/bin/chisel" ]; then
  name="chisel_${CHISEL_VERSION}_linux_amd64.gz"
  rel="https://github.com/jpillora/chisel/releases/download/v$CHISEL_VERSION"
  say "downloading $rel/$name"
  fetch "$rel/$name" "$BASE/tmp/$name"
  fetch "$rel/chisel_${CHISEL_VERSION}_checksums.txt" "$BASE/tmp/checksums.txt"
  (cd "$BASE/tmp" && grep " $name\$" checksums.txt | sha256sum -c - >/dev/null) || die "chisel checksum mismatch"
  mkdir -p "$BASE/bin"
  gunzip -c "$BASE/tmp/$name" > "$BASE/bin/chisel"
  chmod +x "$BASE/bin/chisel"
  rm -rf "$BASE"/tmp/*
fi

# --- build -------------------------------------------------------------------
step "2/3 building the jar"
[ -f "$BASE/src/pom.xml" ] || die "no sources in $BASE/src (deploy.sh pushes them)"
say "building (first build downloads dependencies)"
(cd "$BASE/src" && "$BASE/maven/bin/mvn" -q -B -DskipTests package) >> "$BASE/log/build.log" 2>&1 ||
  { tail -40 "$BASE/log/build.log" >&2; die "build failed"; }
# The runnable jar of a classic Maven build: target/<finalName>.jar, whatever the project calls
# it. The jars Maven writes alongside it (shade's original-, sources, javadoc) are not it.
jar=$(ls -t "$BASE"/src/target/*.jar 2>/dev/null |
        grep -v -e '/original-' -e '[-]sources\.jar$' -e '[-]javadoc\.jar$' | head -1)
[ -n "$jar" ] || die "no jar in src/target after the build (see $BASE/log/build.log)"
say "jar: $(basename "$jar")"
cp "$jar" "$BASE/app.jar"

# --- run -----------------------------------------------------------------------
step "3/3 starting the app on :8080"
running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# The tunnel to MongoDB. A new MongoDB computer (or a branch of it) has a new URL: restart the
# client then. The app retries the database at startup, which covers the tunnel coming up.
# Postgres needs no tunnel: stop a client left by a MongoDB deploy.
if running "$BASE/chisel.pid" && { [ "$DB_KIND" = postgres ] || [ "$(cat "$BASE/chisel.url" 2>/dev/null)" != "$MONGO_TUNNEL_URL" ]; }; then
  say "stopping chisel client"
  kill "$(cat "$BASE/chisel.pid")"; sleep 1
fi
if [ "$DB_KIND" = mongo ] && ! running "$BASE/chisel.pid"; then
  say "starting chisel client -> $MONGO_TUNNEL_URL"
  echo "$MONGO_TUNNEL_URL" > "$BASE/chisel.url"
  AUTH="$CHISEL_USER:$CHISEL_PASSWORD" nohup "$BASE/bin/chisel" client "$MONGO_TUNNEL_URL" \
    127.0.0.1:27017:127.0.0.1:27017 >> "$BASE/log/chisel.log" 2>&1 < /dev/null &
  echo $! > "$BASE/chisel.pid"
  sleep 1
  running "$BASE/chisel.pid" || { tail -30 "$BASE/log/chisel.log" >&2; die "chisel client exited"; }
fi

# Always restart: a deploy means a new jar.
if running "$BASE/app.pid"; then
  say "stopping previous app"
  kill "$(cat "$BASE/app.pid")"; sleep 2
fi
say "starting app on :8080"
# PG_CA_FILE is relative to the sources dir, where deploy.sh unpacks the CA
(cd "$BASE/src" && HOST=0.0.0.0 PORT=8080 nohup java -Xmx256m -jar "$BASE/app.jar" >> "$BASE/log/app.log" 2>&1 < /dev/null &
 echo $! > "$BASE/app.pid")

i=0
until curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  running "$BASE/app.pid" || { tail -30 "$BASE/log/app.log" >&2; die "app exited"; }
  [ $i -lt 60 ] || { tail -30 "$BASE/log/app.log" >&2; die "app not healthy after 60s"; }
  sleep 1
done
say "ready (pid $(cat "$BASE/app.pid"))"
