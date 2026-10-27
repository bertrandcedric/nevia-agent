# Shared by agent.sh, agent-bootstrap.sh, deploy.sh and the computers' bootstraps: sourced by the
# first and third, prepended to the others when they are sent with `nevia exec ... sh -s`.
# Definitions only. TAG prefixes the messages.

JDK_MAJOR=21
MAVEN_VERSION=3.9.12
CHISEL_VERSION=1.10.1

say() { echo "[${TAG:-agent}] $*"; }
step() { printf '\n\033[1m[%s] ─── %s ───\033[0m\n' "${TAG:-agent}" "$*"; }
die() { echo "[${TAG:-agent}] error: $*" >&2; exit 1; }

# load_env <file> [<prefix>]: export its KEY=VALUE lines, unless already set (values never echoed).
# With a prefix (e.g. UC3), <prefix>_KEY sets KEY, before a plain KEY can, and the other use cases'
# UC<n>_ lines are skipped: one file, agent/.env, for every use case.
load_env() {
  [ -f "$1" ] || return 0
  le_prefix=${2:-}
  for le_pass in ${le_prefix:+prefixed} plain; do
    while IFS= read -r line || [ -n "$line" ]; do
      case $line in ''|'#'*) continue ;; esac
      key=${line%%=*}; val=${line#*=}
      case $key in ''|*[!A-Za-z0-9_]*) die "$1: invalid variable name '$key'" ;; esac
      case $le_pass:$key in
        prefixed:"$le_prefix"_*) key=${key#"$le_prefix"_} ;;
        prefixed:*|plain:UC[0-9]*_*) continue ;;
      esac
      case $val in \"*\") val=${val#\"}; val=${val%\"} ;; \'*\') val=${val#\'}; val=${val%\'} ;; esac
      eval "[ -n \"\${$key+x}\" ]" || export "$key=$val"
    done < "$1"
  done
}

# nv: the Nevia CLI, non-interactive (--non-interactive where it has the flag: nevia 0.1.13 and the
# one on a Nevia computer don't, and never prompt). Retried when rate-limited (exit 6, 30 token requests a
# minute), its output kept until it succeeds and its stdin (--file -) replayed. exec passes
# straight through: its stdin is a script, its exit codes have their own meaning.
nv() {
  if [ -z "${NV_NI+x}" ]; then NV_NI=; ! nevia --non-interactive --help >/dev/null 2>&1 || NV_NI=--non-interactive; fi
  # --yes, written first by the scripts: after the subcommand, where it has the flag (nevia 0.1.13
  # has no global --yes, and a command that confirms nothing has none).
  if [ "$1" = --yes ]; then
    shift
    ! nevia "$@" --help 2>/dev/null | grep -q -- '--yes' || set -- "$@" --yes
  fi
  case $1 in exec) nevia $NV_NI "$@"; return ;; esac
  nv_in=; case " $* " in *" --file - "*) nv_in=$(cat) ;; esac
  nv_try=1
  while :; do
    nv_rc=0
    nv_out=$(printf '%s' "$nv_in" | nevia $NV_NI "$@") || nv_rc=$?
    if [ "$nv_rc" -eq 6 ] && [ "$nv_try" -lt 5 ]; then
      say "Nevia rate limit: retrying in 60 s ($nv_try/4)" >&2
      nv_try=$((nv_try + 1)); sleep 60; continue
    fi
    [ -z "$nv_out" ] || printf '%s\n' "$nv_out"
    return "$nv_rc"
  done
}

# status_json [pg] <name>: a computer (or a Postgres database) as JSON, nothing if it doesn't exist.
status_json() {
  if [ "$1" = pg ]; then set -- pg status "$2"; else set -- status "$1"; fi
  rc=0; out=$(nv --json "$@") || rc=$?
  case $rc in
    0) printf '%s' "$out" ;;
    3) ;;
    *) printf '%s\n' "$out" >&2; die "nevia $* failed (exit $rc)" ;;
  esac
}
json_field() { printf '%s' "$1" | jq -r --arg k "$2" 'first(.. | objects | select(has($k)) | .[$k]) // empty'; }

# push_dir <computer> <dir under its home> <local dir> <paths>...: replace that dir with these paths
# (their times not kept: the clocks may differ). Streamed (-i): exec takes at most 1 MiB of stdin otherwise.
push_dir() {
  pd_c=$1 pd_d=$2 pd_l=$3; shift 3
  # macOS tar would add xattrs and AppleDouble files.
  if tar --version 2>/dev/null | grep -q bsdtar; then set -- --no-xattrs --no-mac-metadata "$@"; fi
  COPYFILE_DISABLE=1 tar -czf - -C "$pd_l" "$@" | base64 |
    nevia exec -i "$pd_c" -- sh -c 'cd && rm -rf "$1" && mkdir -p "$1" && base64 -d | tar -xmzf - -C "$1"' sh "$pd_d"
}

fetch() { curl -fsSL "$1" -o "$2"; }

# install_java <dir>: Temurin JDK and Maven into <dir>/jdk and <dir>/maven (once), then on PATH.
install_java() {
  mkdir -p "$1/tmp"
  if [ ! -x "$1/jdk/bin/java" ]; then
    meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$JDK_MAJOR/hotspot?os=linux&architecture=x64&image_type=jdk")
    link=$(printf '%s' "$meta" | jq -r '.[0].binary.package.link')
    say "downloading $link"
    fetch "$link" "$1/tmp/jdk.tgz"
    echo "$(printf '%s' "$meta" | jq -r '.[0].binary.package.checksum')  $1/tmp/jdk.tgz" | sha256sum -c - >/dev/null ||
      die "JDK checksum mismatch"
    mkdir -p "$1/jdk" && tar -xzf "$1/tmp/jdk.tgz" -C "$1/jdk" --strip-components=1 && rm -f "$1/tmp/jdk.tgz"
  fi
  if [ ! -x "$1/maven/bin/mvn" ]; then
    url="https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$MAVEN_VERSION/apache-maven-$MAVEN_VERSION-bin.tar.gz"
    say "downloading $url"
    fetch "$url" "$1/tmp/maven.tgz"
    echo "$(curl -fsSL "$url.sha512" | cut -d' ' -f1)  $1/tmp/maven.tgz" | sha512sum -c - >/dev/null ||
      die "Maven checksum mismatch"
    mkdir -p "$1/maven" && tar -xzf "$1/tmp/maven.tgz" -C "$1/maven" --strip-components=1 && rm -f "$1/tmp/maven.tgz"
  fi
  export JAVA_HOME=$1/jdk PATH=$1/maven/bin:$1/jdk/bin:$PATH
}

# install_chisel <dir>: chisel into <dir>/bin (once).
install_chisel() {
  [ ! -x "$1/bin/chisel" ] || return 0
  mkdir -p "$1/bin" "$1/tmp"
  name="chisel_${CHISEL_VERSION}_linux_amd64.gz"
  rel="https://github.com/jpillora/chisel/releases/download/v$CHISEL_VERSION"
  say "downloading $rel/$name"
  fetch "$rel/$name" "$1/tmp/$name"
  fetch "$rel/chisel_${CHISEL_VERSION}_checksums.txt" "$1/tmp/checksums.txt"
  (cd "$1/tmp" && grep " $name\$" checksums.txt | sha256sum -c - >/dev/null) || die "chisel checksum mismatch"
  gunzip -c "$1/tmp/$name" > "$1/bin/chisel" && chmod +x "$1/bin/chisel" && rm -f "$1/tmp/$name" "$1/tmp/checksums.txt"
}

running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
