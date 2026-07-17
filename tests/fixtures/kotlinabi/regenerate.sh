#!/usr/bin/env bash
# Regenerate the kotlin-abi .class fixtures from src/*.kt.
#
# The matrix (see each source's header comment for the why):
#   plain_base / plain_impl          no inline: impl-only change -> SAME digest
#   facade_base / facade_impl        file facade, no inline      -> SAME digest
#   inline_base / inline_bodychange  inline fun body change      -> digest CHANGES
#   inline_base / inline_lineshift   line shift above inline fun -> digest CHANGES
#   accessor                         inline property getter      -> byte ("raw") mode
#   nested_owner + nested_synthetic  inline fun w/ anonymous object; the
#                                    synthetic nested class is byte-counted
#                                    via the jar-level enclosure rule
#   multifile_facade + multifile_part  kind-4 facade fail-closes to raw;
#                                      kind-5 part parses as Package -> abi
#   futuremv                         plain_base with @Metadata's major version
#                                    byte-patched to 9 -> fail-closed to raw
#
# The .class bytes are checked in so tests need no Kotlin toolchain. After
# regenerating, update the pinned digests in tests/test_kotlinabi.py and
# rust/brix-core/tests/identity.rs (a change means the algorithm changed —
# bump KotlinAbiIdentity.version AND the Rust tag to match).
#
# Usage:  KOTLINC=~/Projects/brix_test/kotlinc/bin/kotlinc tests/fixtures/kotlinabi/regenerate.sh
set -euo pipefail
cd "$(dirname "$0")"
KOTLINC="${KOTLINC:-kotlinc}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Variants of one fixture family are compiled under the SAME canonical file
# name: kotlinc embeds the source file name in SourceFile attributes and SMAP
# (@SourceDebugExtension) annotations, and same-vs-different comparisons must
# be about the declared change, not the file name.
compile() { # <fixture-source-stem> <canonical-file-name> <compiled-relative-path> <output-name>
    local stem=$1 file=$2 rel=$3 out=$4
    if [ ! -e "$tmp/$stem" ]; then
        mkdir -p "$tmp/$stem"
        cp "src/$stem.kt" "$tmp/$stem/$file"
        "$KOTLINC" -jvm-target 17 -d "$tmp/$stem" "$tmp/$stem/$file"
    fi
    cp "$tmp/$stem/$rel" "$out.class"
    echo "  $out.class  <-  src/$stem.kt ($rel)"
}

compile plain_base        plain.kt     abidemo/Greeter.class              plain_base
compile plain_impl        plain.kt     abidemo/Greeter.class              plain_impl
compile facade_base       facade.kt    abidemo/FacadeKt.class             facade_base
compile facade_impl       facade.kt    abidemo/FacadeKt.class             facade_impl
compile inline_base       inline.kt    abidemo/InlineKt.class             inline_base
compile inline_lineshift  inline.kt    abidemo/InlineKt.class             inline_lineshift
compile inline_bodychange inline.kt    abidemo/InlineKt.class             inline_bodychange
compile accessor          accessor.kt  abidemo/Holder.class               accessor
compile nested            nested.kt    abidemo/NestedKt.class             nested_owner
compile nested            nested.kt    'abidemo/NestedKt$runner$1.class'  nested_synthetic
compile nested_impl       nested.kt    'abidemo/NestedKt$runner$1.class'  nested_synthetic_impl
compile multifile         multifile.kt abidemo/Utils.class                multifile_facade
compile multifile         multifile.kt abidemo/Utils__MultifileKt.class   multifile_part

# futuremv: plain_base with the @Metadata metadata-version major byte-patched
# to 9 (an uncalibrated major -> the lens must fail closed to byte counting).
# The patch rewrites every CONSTANT_Integer pool entry whose value equals the
# real major — in this fixture that integer is only used by @Metadata's mv.
python3 - <<'EOF'
import struct, sys
sys.path.insert(0, "../../../src")
from brix import kotlinabi, jvmabi

data = bytearray(open("plain_base.class", "rb").read())
elements = kotlinabi._metadata_elements(jvmabi.class_abi(bytes(data)))
major = kotlinabi._int_array_element(elements[kotlinabi._ELEM_MV])[0]

# Walk the constant pool recording each entry's byte offset (JVMS 4.4).
p = 10  # magic(4) + minor(2) + major(2) + count(2)
count = struct.unpack_from(">H", data, 8)[0]
i, patched = 1, 0
while i < count:
    tag = data[p]
    if tag == 1:  # Utf8
        size = 3 + struct.unpack_from(">H", data, p + 1)[0]
    elif tag == 3:  # Integer
        if struct.unpack_from(">i", data, p + 1)[0] == major:
            struct.pack_into(">i", data, p + 1, 9)
            patched += 1
        size = 5
    elif tag in (4,):  # Float
        size = 5
    elif tag in (5, 6):  # Long/Double: two slots
        size, i = 9, i + 1
    elif tag in (7, 8, 16, 19, 20):
        size = 3
    elif tag in (9, 10, 11, 12, 17, 18):
        size = 5
    elif tag == 15:
        size = 4
    else:
        raise SystemExit(f"unknown pool tag {tag}")
    p += size
    i += 1
assert patched, "no Integer pool entry matched the metadata major version"
open("futuremv.class", "wb").write(bytes(data))
mode = kotlinabi._class_mode(jvmabi.class_abi(bytes(data)))
assert mode == "raw", f"futuremv must fail closed, got {mode}"
print(f"  futuremv.class  <-  plain_base.class (mv major {major} -> 9, {patched} pool entries)")
EOF

echo "regenerated all kotlin-abi fixtures"
