# Sourced by the scripts both agents run on Nevia computers (Debian 13, bash), from ~/.nevia-agent/lib.sh, and by
# their Python on the agent computer (core.agent.host.lib): messages, apt, downloads checked against their
# publisher's checksum, and installers run once.
# Definitions only. TAG prefixes the messages.

say() { echo "[${TAG:-agent}] $*"; }
die() { echo "[${TAG:-agent}] error: $*" >&2; exit 1; }
fetch() { curl -fsSL --retry 3 --proto =https "$1" -o "$2"; }
running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# apt_install PACKAGE...: Debian packages, quietly (from /, as apt wants).
apt_install() {
  (cd / && sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    --no-install-recommends "$@" >/dev/null) || die "could not install $*"
}

# fetch_checked URL CHECKSUM DEST: URL into DEST, or nothing and an error when it doesn't match CHECKSUM:
# sha256:HEX, sha512:HEX, or sha512-BASE64 (npm's integrity).
fetch_checked() {
  local url=$1 sum=$2 dest=$3 have
  fetch "$url" "$dest"
  case $sum in
    sha256:*) have=sha256:$(sha256sum "$dest" | cut -d' ' -f1) ;;
    sha512:*) have=sha512:$(sha512sum "$dest" | cut -d' ' -f1) ;;
    sha512-*) have=sha512-$(openssl dgst -sha512 -binary "$dest" | base64 -w0) ;;
    *) rm -f "$dest"; die "unknown checksum for $url: $sum" ;;
  esac
  [ "$have" = "$sum" ] || { rm -f "$dest"; die "$(basename "$url"): checksum mismatch"; }
}

# install_java DIR JDK_MAJOR MAVEN_VERSION: Temurin JDK and Maven into DIR/jdk and DIR/maven, then on PATH.
install_java() {
  local dir=$1 jdk=$2 maven=$3 meta url
  mkdir -p "$dir/tmp"
  if [ ! -x "$dir/jdk/bin/java" ]; then
    meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$jdk/hotspot?os=linux&architecture=x64&image_type=jdk")
    url=$(jq -r '.[0].binary.package.link' <<< "$meta")
    say "downloading $url"
    fetch_checked "$url" "sha256:$(jq -r '.[0].binary.package.checksum' <<< "$meta")" "$dir/tmp/jdk.tgz"
    mkdir -p "$dir/jdk" && tar -xzf "$dir/tmp/jdk.tgz" -C "$dir/jdk" --strip-components=1 && rm -f "$dir/tmp/jdk.tgz"
  fi
  if [ ! -x "$dir/maven/bin/mvn" ]; then
    url="https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$maven/apache-maven-$maven-bin.tar.gz"
    say "downloading $url"
    fetch_checked "$url" "sha512:$(curl -fsSL "$url.sha512" | cut -d' ' -f1)" "$dir/tmp/maven.tgz"
    mkdir -p "$dir/maven" && tar -xzf "$dir/tmp/maven.tgz" -C "$dir/maven" --strip-components=1 && rm -f "$dir/tmp/maven.tgz"
  fi
  export JAVA_HOME=$dir/jdk PATH=$dir/maven/bin:$dir/jdk/bin:$PATH
}

# install_chisel DIR VERSION: chisel into DIR/bin.
install_chisel() {
  local dir=$1 version=$2 name rel
  [ ! -x "$dir/bin/chisel" ] || return 0
  mkdir -p "$dir/bin" "$dir/tmp"
  name="chisel_${version}_linux_amd64.gz"
  rel="https://github.com/jpillora/chisel/releases/download/v$version"
  say "downloading $rel/$name"
  fetch_checked "$rel/$name" "sha256:$(curl -fsSL "$rel/chisel_${version}_checksums.txt" | awk -v n="$name" '$2 == n { print $1 }')" \
    "$dir/tmp/$name"
  gunzip -c "$dir/tmp/$name" > "$dir/bin/chisel" && chmod +x "$dir/bin/chisel" && rm -f "$dir/tmp/$name"
}

# app_jar DIR: the runnable jar Maven built in DIR/target, not the ones it writes alongside it.
app_jar() {
  ls -t "$1"/target/*.jar 2>/dev/null | grep -v -e '/original-' -e '-sources\.jar$' -e '-javadoc\.jar$' | head -1
}
