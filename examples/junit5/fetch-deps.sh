#!/usr/bin/env bash
# Materialize JUnit 5's external compile dependencies into the checkout's
# lib/. brix has no dependency resolver by design — you hand it the jars —
# so we fetch the two the platform modules need straight from Maven Central.
# (The full Gradle build resolves these; we skip Gradle entirely for the
# compile graph and use it only if/when we benchmark against it.)
set -euo pipefail

ROOT="${BRIX_JUNIT5_ROOT:-$HOME/Projects/brix_test/junit5}"
LIB="$ROOT/lib"
CENTRAL="https://repo1.maven.org/maven2"

mkdir -p "$LIB"
fetch() { # <group-path> <artifact> <version>
  local url="$CENTRAL/$1/$2/$3/$2-$3.jar"
  echo "  $2-$3.jar"
  curl -fsSL -o "$LIB/$2-$3.jar" "$url"
}

echo "fetching into $LIB"
fetch org/apiguardian apiguardian-api 1.1.2
fetch org/opentest4j opentest4j 1.3.0
# testkit only: it exposes AssertJ in its public API; assertj-core needs
# byte-buddy at compile scope.
fetch org/assertj assertj-core 3.27.3
fetch net/bytebuddy byte-buddy 1.15.11
echo "done"
