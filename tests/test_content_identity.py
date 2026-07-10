"""Content-identity fingerprints (jvm-abi): the parser and the engine wiring.

Fixtures under fixtures/jvmabi/ are `.class` files of the same class
(abidemo.Lib) compiled from different source versions — checked in so these
tests need no JDK. Regenerate with fixtures/jvmabi/regenerate.sh.
"""

import zipfile
from pathlib import Path

import pytest

from brix import (
    BuildMetadata,
    ExternalProcessAction,
    Graph,
    NodeContext,
    TimeAndHashChangeDetector,
    identity,
    jvmabi,
)
from brix.incremental import action_fingerprint

FIX = Path(__file__).parent / "fixtures" / "jvmabi"

# The golden contract: the exact ABI digest of each fixture. A change here means
# the ABI algorithm changed (bump JvmAbiIdentity.version to match) or, later,
# that the Rust jvm-abi implementation disagrees with Python. Both are things a
# test must catch, not wave through.
EXPECTED = {
    "base": "70286f3e666e92342509bcbd9f7f4855ccf3d20c4b6e025ea0f7b7899d137b18",
    "impl": "70286f3e666e92342509bcbd9f7f4855ccf3d20c4b6e025ea0f7b7899d137b18",
    "addmethod": "90c437bf23a6b8c1289b441eec393dcbeee425bf9fff20f05d65f69ecaae441f",
    "constant": "36f7d159f8b658b7b391441a5acc616f76169868764c2c13a1e14dbf0b333b13",
    "annotation": "140b9c778e2bdccc225e15f24c7e11f470777ecc8fec03b656d6f1fe3ecb48d4",
    # A String-valued annotation element (@Since("1.13")). This is the case
    # that used to crash the parser (the 's' element value indexes a Utf8
    # directly, not a String constant) so real Java — every @API(since=...)
    # class — silently fell back to :raw byte hashing. Regression guard.
    "strannotation": "0c7e6d76dd379b1a5a9b066729c009090977264e93d9130a6ca09c8b0b79c434",
}


def cls(name: str) -> bytes:
    return (FIX / f"{name}.class").read_bytes()


def jar(tmp: Path, fixture: str, name: str = "util.jar") -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("abidemo/Lib.class", cls(fixture))
    return p


# -- the parser --------------------------------------------------------------


def test_pinned_class_digests():
    for name, digest in EXPECTED.items():
        assert jvmabi.digest_class(cls(name)) == digest, name


def test_implementation_only_change_hashes_equal():
    # Different bodies, different private field init, different private method:
    # the API is identical, so the ABI digest must be too.
    assert jvmabi.digest_class(cls("base")) == jvmabi.digest_class(cls("impl"))


def test_api_changes_each_hash_differently():
    base = jvmabi.digest_class(cls("base"))
    changed = {jvmabi.digest_class(cls(n)) for n in ("addmethod", "constant", "annotation")}
    assert base not in changed
    assert len(changed) == 3  # add-method, constant-value, annotation all distinct


def test_string_valued_annotation_is_captured():
    # The regression: an @Since("1.13")-style String element used to make the
    # parser raise (leaking raw Utf8 bytes into the ABI) so the whole class
    # fell back to :raw. It must now parse cleanly and be ABI-relevant —
    # distinct from base and from the value-less @Deprecated fixture.
    d = jvmabi.digest_class(cls("strannotation"))
    assert d not in (jvmabi.digest_class(cls("base")), jvmabi.digest_class(cls("annotation")))


def test_jar_abi_impl_only_equal(tmp_path):
    assert jvmabi.digest_jar(jar(tmp_path, "base", "a.jar")) == jvmabi.digest_jar(
        jar(tmp_path, "impl", "b.jar")
    )


def test_jar_abi_api_change_differs(tmp_path):
    assert jvmabi.digest_jar(jar(tmp_path, "base", "a.jar")) != jvmabi.digest_jar(
        jar(tmp_path, "addmethod", "b.jar")
    )


def test_digest_autodetects_jar_vs_class(tmp_path):
    j = jar(tmp_path, "base")
    assert jvmabi.digest(j) == jvmabi.digest_jar(j)
    cf = tmp_path / "base.class"
    cf.write_bytes(cls("base"))
    assert jvmabi.digest(cf) == jvmabi.digest_class(cls("base"))


def test_malformed_class_raises():
    with pytest.raises(jvmabi.ClassFormatError):
        jvmabi.digest_class(b"not a class file")


# -- the engine wiring -------------------------------------------------------


def _dependent_fingerprint(root: Path, fixture: str) -> str:
    """The fingerprint of an action that depends on util.jar with jvm-abi
    identity, as the incremental/cache layers would compute it."""
    jar(root, fixture)
    graph = Graph()
    data = graph.data
    action = ExternalProcessAction(
        name="compile app",
        command_line="{javac} -cp {cp} App.java",
        inputs={
            "javac": data.tool("java"),
            "cp": data.file("util.jar", identity="jvm-abi"),
        },
        outputs={},
    )
    return action_fingerprint(
        action, NodeContext.for_root(root), TimeAndHashChangeDetector(root)
    )


def test_dependent_fingerprint_ignores_impl_change(tmp_path):
    assert _dependent_fingerprint(tmp_path / "a", "base") == _dependent_fingerprint(
        tmp_path / "b", "impl"
    )


def test_dependent_fingerprint_reacts_to_api_change(tmp_path):
    base = _dependent_fingerprint(tmp_path / "a", "base")
    for change in ("addmethod", "constant", "annotation"):
        assert base != _dependent_fingerprint(tmp_path / change, change), change


def test_non_jar_with_identity_falls_back_to_raw(tmp_path):
    target = tmp_path / "x.jar"
    target.write_text("not a jar at all")
    graph = Graph()
    action = ExternalProcessAction(
        name="x",
        command_line="{f}",
        inputs={"f": graph.data.file("x.jar", identity="jvm-abi")},
        outputs={},
    )

    def fp():
        return action_fingerprint(
            action, NodeContext.for_root(tmp_path), TimeAndHashChangeDetector(tmp_path)
        )

    first = fp()
    assert isinstance(first, str) and len(first) == 64  # did not crash
    # The :raw fallback is a byte hash, so different bytes -> different key.
    target.write_text("still not a jar, but different bytes")
    assert first != fp()


def test_unknown_identity_is_loud(tmp_path):
    (tmp_path / "x.jar").write_text("data")
    graph = Graph()
    action = ExternalProcessAction(
        name="x",
        command_line="{f}",
        inputs={"f": graph.data.file("x.jar", identity="no-such-identity")},
        outputs={},
    )
    with pytest.raises(ValueError):
        action_fingerprint(
            action, NodeContext.for_root(tmp_path), TimeAndHashChangeDetector(tmp_path)
        )


# -- registry and metadata cache ---------------------------------------------


def test_registry_and_tag():
    plugin = identity.get("jvm-abi")
    assert plugin.version == "1"
    assert identity.tag(plugin) == "jvm-abi@1"
    with pytest.raises(ValueError):
        identity.get("does-not-exist")


def test_metadata_caches_identity_alongside_byte_hash(tmp_path):
    meta = BuildMetadata(tmp_path, "meta")
    sig = [123, 45]
    meta.store_hash("/p", sig, "BYTES")
    meta.store_identity("/p", sig, "jvm-abi@1", "ABI")
    # Both live in one stat-validated entry.
    assert meta.cached_hash("/p", sig) == "BYTES"
    assert meta.cached_identity("/p", sig, "jvm-abi@1") == "ABI"
    # A changed signature invalidates both.
    assert meta.cached_hash("/p", [999, 9]) is None
    assert meta.cached_identity("/p", [999, 9], "jvm-abi@1") is None
    # Re-hashing under a new signature drops the now-stale identity digest.
    meta.store_hash("/p", [999, 9], "NEWBYTES")
    assert meta.cached_identity("/p", [999, 9], "jvm-abi@1") is None


def test_identity_digest_cached_by_signature(tmp_path):
    j = jar(tmp_path, "base")

    calls: list = []

    class Spy:
        name = "spy-identity"
        version = "9"

        def digest(self, path):
            calls.append(path)
            return "DIGEST"

    identity.register(Spy())
    try:
        detector = TimeAndHashChangeDetector(tmp_path)
        first = detector.identity_digest(j, identity.get("spy-identity"))
        second = detector.identity_digest(j, identity.get("spy-identity"))
        assert first == second == "DIGEST"
        assert len(calls) == 1  # the second call was served from the metadata cache
    finally:
        identity._REGISTRY.pop("spy-identity", None)
