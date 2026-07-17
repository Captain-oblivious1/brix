"""The kotlin-abi content identity: the parser, the modes, and the jar rules.

Fixtures under fixtures/kotlinabi/ are `.class` files compiled by kotlinc from
src/*.kt — checked in so these tests need no Kotlin toolchain. Regenerate with
fixtures/kotlinabi/regenerate.sh (each source's header comment says what its
case proves).

The lens's contract (see brix/kotlinabi.py): jvm-abi counting everywhere it is
sound, whole-class byte counting exactly where Kotlin copies implementation
into consumers — classes with inline functions or inline property accessors,
classes nested under them, and any metadata the parser will not vouch for.
"""

import zipfile
from pathlib import Path

import pytest
import brix.jvmabi as jvmabi
import brix.kotlinabi as kotlinabi
from brix import identity

FIX = Path(__file__).parent / "fixtures" / "kotlinabi"
JAVA_FIX = Path(__file__).parent / "fixtures" / "jvmabi"

# The golden contract: the exact kotlin-abi digest of each fixture. A change
# here means the algorithm changed (bump KotlinAbiIdentity.version AND the
# Rust lens tag to match) or that the Rust implementation disagrees with
# Python. Both must be caught, not waved through.
EXPECTED = {
    "plain_base": "976787a605e8b3562939ef662e1719b19b61d104e19043a7b1644f16e5e1ea04",
    "plain_impl": "976787a605e8b3562939ef662e1719b19b61d104e19043a7b1644f16e5e1ea04",
    "facade_base": "f4a70838ca81f2736ee9fde8eb862f0f17f7d71977ff27dd63569b3b38277592",
    "facade_impl": "f4a70838ca81f2736ee9fde8eb862f0f17f7d71977ff27dd63569b3b38277592",
    "inline_base": "19b02b5477b49a08c1608a3910965b5a483273e69aff1e41199ea6c4071fd12c",
    "inline_lineshift": "89c1d52a7630211c51456a383aa3d72faabce1a348aa2b8ebf0e3edfa659300a",
    "inline_bodychange": "386013fab5298b8670b7d87892cca0875e731c505b8a992bd01d9874bb507bc3",
    "accessor": "5c55f8043971f42dbb00933ba7a25ee8bce203e2be7398962508e19a6da1a53e",
    "nested_owner": "91fca98211b1c3c2c58bd76644c1ab581440e823aaed05f1f5d69d129d483e79",
    "nested_synthetic": "279c4a06cc844e7a2ce0231e155d80172dcdbd2369c36162ebfd1c6d96a6ca81",
    "nested_synthetic_impl": "79be08de901b7ddabc113d5e5dcc032508c7aecb44b5c312601c39d874095503",
    "multifile_facade": "900866a1ca5d1165e531762c30c54c6952be128ad3dfcd69dbdef4ec1f6a30c2",
    "multifile_part": "ee9fd3d9aaa8e43b700ae9ec36b38449d24121dd55e567515753adac17b5e769",
    "futuremv": "ddb25d3e8c228dff72e6e1868742cea1cab072470a57c2f2dbd4ab876a29141d",
}


def cls(name: str) -> bytes:
    return (FIX / f"{name}.class").read_bytes()


def mode_of(name: str) -> str:
    return kotlinabi._class_mode(jvmabi.class_abi(cls(name)))


def jar(tmp: Path, entries: dict, name: str = "dep.jar") -> Path:
    p = tmp / name
    with zipfile.ZipFile(p, "w") as z:
        for entry, data in entries.items():
            z.writestr(entry, data)
    return p


# -- pinned digests -----------------------------------------------------------


def test_pinned_class_digests():
    for name, digest in EXPECTED.items():
        assert kotlinabi.digest_class(cls(name)) == digest, name


# -- per-class modes ----------------------------------------------------------


def test_modes_follow_the_inline_verdict():
    assert mode_of("plain_base") == "abi"  # Kotlin class, no inline
    assert mode_of("facade_base") == "abi"  # file facade (kind 2), no inline
    assert mode_of("multifile_part") == "abi"  # multifile part (kind 5), no inline
    assert mode_of("inline_base") == "raw"  # inline fun
    assert mode_of("accessor") == "raw"  # inline property getter
    assert mode_of("nested_owner") == "raw"  # declares the inline fun
    # The synthetic's own metadata (kind 3) says nothing about inline — its
    # byte counting comes from the jar-level enclosure rule, not _class_mode.
    assert mode_of("nested_synthetic") == "abi"


def test_fail_closed_modes():
    assert mode_of("multifile_facade") == "raw"  # kind 4: not vouched for
    assert mode_of("futuremv") == "raw"  # uncalibrated metadata major version


def test_java_class_without_metadata_keeps_jvm_abi_semantics():
    # Reuse the jvm-abi fixtures: an implementation-only Java change must be
    # invisible through kotlin-abi too.
    base = (JAVA_FIX / "base.class").read_bytes()
    impl = (JAVA_FIX / "impl.class").read_bytes()
    assert kotlinabi._class_mode(jvmabi.class_abi(base)) == "abi"
    assert kotlinabi.digest_class(base) == kotlinabi.digest_class(impl)


# -- the leak channels, as digests ---------------------------------------------


def test_no_inline_implementation_change_hashes_equal():
    assert kotlinabi.digest_class(cls("plain_base")) == kotlinabi.digest_class(cls("plain_impl"))
    assert kotlinabi.digest_class(cls("facade_base")) == kotlinabi.digest_class(cls("facade_impl"))


def test_inline_body_change_hashes_differently():
    assert kotlinabi.digest_class(cls("inline_base")) != kotlinabi.digest_class(cls("inline_bodychange"))


def test_inline_line_shift_hashes_differently():
    # Nothing but line numbers changed — but inlined code carries them into
    # consumers (SMAP), so the lens must see it. jvm-abi does not: that is the
    # gap this lens exists to close, without giving up the skip elsewhere.
    assert kotlinabi.digest_class(cls("inline_base")) != kotlinabi.digest_class(cls("inline_lineshift"))
    assert jvmabi.digest_class(cls("inline_base")) == jvmabi.digest_class(cls("inline_lineshift"))


# -- the jar level -------------------------------------------------------------


def test_enclosure_rule_byte_counts_synthetics_under_inline(tmp_path):
    # The two synthetics differ only in the anonymous object's body — bytes a
    # consumer copies when inlining runner(). Their jvm-abi digests are equal
    # (the guard: the change is invisible to plain ABI counting), so only the
    # enclosure rule — NestedKt$runner$1 nests under the inline-flagged
    # NestedKt — makes the jar digests differ.
    assert jvmabi.digest_class(cls("nested_synthetic")) == jvmabi.digest_class(cls("nested_synthetic_impl"))
    a = jar(tmp_path, {
        "abidemo/NestedKt.class": cls("nested_owner"),
        "abidemo/NestedKt$runner$1.class": cls("nested_synthetic"),
    }, "a.jar")
    b = jar(tmp_path, {
        "abidemo/NestedKt.class": cls("nested_owner"),
        "abidemo/NestedKt$runner$1.class": cls("nested_synthetic_impl"),
    }, "b.jar")
    assert kotlinabi.digest_jar(a) != kotlinabi.digest_jar(b)


def test_nested_class_without_inline_ancestor_keeps_abi_counting(tmp_path):
    # The same synthetic pair, but WITHOUT the inline-flagged owner in the
    # jar: nothing flags the $-ancestor, so the synthetics count by ABI and
    # the body-only difference stays invisible. (In a real jar the owner is
    # always present; this isolates the enclosure rule itself.)
    a = jar(tmp_path, {"abidemo/NestedKt$runner$1.class": cls("nested_synthetic")}, "a.jar")
    b = jar(tmp_path, {"abidemo/NestedKt$runner$1.class": cls("nested_synthetic_impl")}, "b.jar")
    assert kotlinabi.digest_jar(a) == kotlinabi.digest_jar(b)


def test_kotlin_module_entries_count_by_bytes(tmp_path):
    base = {"abidemo/Greeter.class": cls("plain_base")}
    plain = jar(tmp_path, base, "plain.jar")
    with_module = jar(tmp_path, {**base, "META-INF/app.kotlin_module": b"v1"}, "m1.jar")
    module_changed = jar(tmp_path, {**base, "META-INF/app.kotlin_module": b"v2"}, "m2.jar")
    assert kotlinabi.digest_jar(plain) != kotlinabi.digest_jar(with_module)
    assert kotlinabi.digest_jar(with_module) != kotlinabi.digest_jar(module_changed)


def test_other_resources_are_ignored(tmp_path):
    a = jar(tmp_path, {"abidemo/Greeter.class": cls("plain_base")}, "a.jar")
    b = jar(tmp_path, {
        "abidemo/Greeter.class": cls("plain_base"),
        "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n",
        "banner.txt": b"hello",
    }, "b.jar")
    assert kotlinabi.digest_jar(a) == kotlinabi.digest_jar(b)


# -- entry points and registration ----------------------------------------------


def test_digest_autodetects_jar_vs_class(tmp_path):
    p = jar(tmp_path, {"abidemo/Greeter.class": cls("plain_base")})
    assert kotlinabi.digest(p) == kotlinabi.digest_jar(p)
    single = tmp_path / "Greeter.class"
    single.write_bytes(cls("plain_base"))
    assert kotlinabi.digest(single) == EXPECTED["plain_base"]


def test_bare_nested_class_file_counts_by_bytes():
    # A lone $-nested .class has no jar to check its ancestors in, so it must
    # fail closed: the body-only synthetic pair digests differently here.
    assert kotlinabi.digest_class(cls("nested_synthetic")) != kotlinabi.digest_class(cls("nested_synthetic_impl"))


def test_malformed_class_raises():
    with pytest.raises(jvmabi.ClassFormatError):
        kotlinabi.digest_class(b"\xde\xad\xbe\xef not a class file")


def test_registered_as_builtin():
    plugin = identity.get("kotlin-abi")
    assert identity.tag(plugin) == "kotlin-abi@1"
    assert "kotlin-abi" in identity.builtins()
