#!/usr/bin/env bash
# Regenerate the jvm-abi .class fixtures from src/*.java.
#
# Each source declares the SAME class (abidemo.Lib) from a different version,
# so the compiled Lib.class files form a matrix of ABI changes:
#   base        baseline
#   impl        implementation-only change (bodies + private members) -> same ABI
#   addmethod   adds a public method                                  -> ABI changes
#   constant    changes a static-final constant value                 -> ABI changes
#   annotation  adds an annotation on a public method                 -> ABI changes
#
# The .class bytes are checked in so tests need no JDK. After regenerating,
# update the pinned digests in tests/test_content_identity.py (a change there
# means the ABI algorithm changed -- bump JvmAbiIdentity.version to match).
#
# Usage:  tests/fixtures/jvmabi/regenerate.sh
set -euo pipefail
cd "$(dirname "$0")"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for v in base impl addmethod constant annotation strannotation; do
    mkdir -p "$tmp/$v"
    cp "src/$v.java" "$tmp/$v/Lib.java"   # a public class must live in Lib.java
    javac -d "$tmp/$v" "$tmp/$v/Lib.java"
    cp "$tmp/$v/abidemo/Lib.class" "$v.class"
done
echo "regenerated: base impl addmethod constant annotation strannotation (.class)"
