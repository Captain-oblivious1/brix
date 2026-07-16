"""TypeScript `.d.ts` content identity (ts-dts): the normalizer and the engine
wiring. The pruning property — impl/comment churn doesn't invalidate consumers,
an API change does — is the ABI multiplier for a Java+TS monorepo's cache."""

from pathlib import Path
import brix.tsdts as tsdts

from brix import (
    ExternalProcessAction,
    Graph,
    NodeContext,
    identity,
)
from brix.data import FileData
from brix.incremental import action_fingerprint
from brix.metadata import TimeAndHashChangeDetector


class DtsFile(FileData):
    """A `.d.ts` compared through the ts-dts lens. ts-dts is a built-in ready
    for a future TS layer that would own this typed atom (like `brix.java`
    owns `AbiFile`); until then the test declares it directly."""

    content_key = "ts-dts"


BASE = """\
// the public API of the util module
export declare function greet(name: string): string;
export interface Options {
    loud: boolean;
}
"""

# Same public surface, different doc comments and formatting only. tsc would
# emit this for an implementation-only change (declarations are impl-free); a
# hand reformat / JSDoc edit lands here too.
IMPL_ONLY = """\
/** Greets a person. Reworded doc, reindented — API unchanged. */
export declare function greet(name:    string): string;

export interface Options { loud: boolean; }
"""

# A real API change: a new optional parameter.
API_CHANGE = """\
export declare function greet(name: string, times?: number): string;
export interface Options {
    loud: boolean;
}
"""


# -- the normalizer ----------------------------------------------------------


def test_comment_and_format_changes_hash_equal(tmp_path):
    a = tmp_path / "a.d.ts"
    b = tmp_path / "b.d.ts"
    a.write_text(BASE)
    b.write_text(IMPL_ONLY)
    assert tsdts.digest(a) == tsdts.digest(b)


def test_api_change_hashes_differently(tmp_path):
    a = tmp_path / "a.d.ts"
    c = tmp_path / "c.d.ts"
    a.write_text(BASE)
    c.write_text(API_CHANGE)
    assert tsdts.digest(a) != tsdts.digest(c)


def test_whitespace_inside_string_literal_is_significant():
    # "a  b" and "a b" are different string-literal *types*; formatting collapse
    # must not conflate them, while whitespace outside strings is normalized.
    assert tsdts.normalize('export type T = "a  b";') != tsdts.normalize('export type T = "a b";')
    assert tsdts.normalize("export  type   T = X;") == tsdts.normalize("export type T = X;")


def test_double_slash_inside_string_is_not_a_comment():
    # A URL literal type contains `//` that must not be treated as a comment.
    kept = tsdts.normalize('export const u: "http://x/y";')
    assert "http://x/y" in kept


# -- the engine wiring -------------------------------------------------------


def _dependent_fingerprint(root: Path, source: str) -> str:
    """Fingerprint of an action consuming api.d.ts with ts-dts identity."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "api.d.ts").write_text(source)
    graph = Graph()
    data = graph.data
    action = ExternalProcessAction(
        name="build consumer",
        command_line="{tsc} -p .",
        inputs={
            "tsc": data.tool("tsc"),
            "api": DtsFile("api.d.ts"),
        },
        outputs={},
    )
    return action_fingerprint(action, NodeContext.for_root(root), TimeAndHashChangeDetector(root))


def test_dependent_fingerprint_ignores_impl_and_doc_change(tmp_path):
    assert _dependent_fingerprint(tmp_path / "a", BASE) == _dependent_fingerprint(
        tmp_path / "b", IMPL_ONLY
    )


def test_dependent_fingerprint_reacts_to_api_change(tmp_path):
    assert _dependent_fingerprint(tmp_path / "a", BASE) != _dependent_fingerprint(
        tmp_path / "c", API_CHANGE
    )


def test_registry_and_tag():
    plugin = identity.get("ts-dts")
    assert plugin.version == "1"
    assert identity.tag(plugin) == "ts-dts@1"


def test_undecodable_file_falls_back_to_raw(tmp_path):
    """A non-text file under ts-dts identity must not crash — digest raises and
    the engine byte-hashes under :raw, so different bytes still differ."""
    target = tmp_path / "api.d.ts"
    target.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    graph = Graph()
    action = ExternalProcessAction(
        name="x",
        command_line="{f}",
        inputs={"f": DtsFile("api.d.ts")},
        outputs={},
    )

    def fp():
        return action_fingerprint(
            action, NodeContext.for_root(tmp_path), TimeAndHashChangeDetector(tmp_path)
        )

    first = fp()
    assert isinstance(first, str) and len(first) == 64  # did not crash
    target.write_bytes(b"\xff\xfe\x00\x02 different bytes")
    assert first != fp()
