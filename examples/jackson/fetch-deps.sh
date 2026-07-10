#!/usr/bin/env bash
# jackson-core has zero runtime deps; its tests need JUnit 5 (via the console
# standalone jar, which is also the runner) and AssertJ (+ byte-buddy). brix
# has no resolver, so fetch them from Maven Central into the checkout's lib/.
set -euo pipefail

ROOT="${BRIX_JACKSON_ROOT:-$HOME/Projects/brix_test/jackson-core}"
LIB="$ROOT/lib"
CENTRAL="https://repo1.maven.org/maven2"

mkdir -p "$LIB"
fetch() { echo "  $2-$3.jar"; curl -fsSL -o "$LIB/$2-$3.jar" "$CENTRAL/$1/$2/$3/$2-$3.jar"; }

echo "fetching into $LIB"
fetch org/junit/platform junit-platform-console-standalone 1.11.4
fetch org/assertj assertj-core 3.27.3
fetch net/bytebuddy byte-buddy 1.15.11
fetch ch/randelshofer fastdoubleparser 2.0.1   # jackson-core's one compile dep
echo "done"
