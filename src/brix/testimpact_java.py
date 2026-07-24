"""The Java provider for test-impact selection (design:
docs/test-impact-selection.md).

Fills the per-language seam `testimpact.Provider` defines, at Java's coarse
**binding unit: the class**. Everything comes from bytecode brix has already
built — `jvmabi.class_refs` reads each class file's constant pool — so this is
re-pointing a graph brix already discovers, not new analysis.

Why class-closure is a *superset* of what a test can execute:

* The JVM resolves every symbolic reference through the constant pool, so a
  class cannot statically bind to a type its pool omits. Virtual dispatch does
  not escape this: the test only ever reaches an implementation that something
  in its closure instantiated, and the `new` site names the concrete class.
* The escapes are binding-by-name at run time (reflection, `ClassLoader`,
  `ServiceLoader`, JNI) and binding into code we were not given. Both are
  handled the same way — the referencing class is marked **dynamic**, and the
  core then runs *every* test reaching it, unconditionally.

That second case matters: a class whose reference we cannot resolve to any
scanned class (and that is not JDK platform code) might be a framework that
reflects back into the code under test, so we must not assume it inert. It
degrades **locally** — the tests touching that class always run, the rest still
select — rather than collapsing the whole suite to a full run.

**Direction is what separates a real escape from a phantom one.** The test
runner reflects to *find* the test — it is the caller, sitting above the test,
and it loads a name brix itself supplied on the command line. Everything the
test does from there is ordinary static Java. So the runner's internals were
never part of what a test depends on; a reference walk that wanders into them
(via, say, `Assertions` sharing a utility class with the launcher) is following
reference edges that are not call edges, and treating what it finds there as
"unknowable" would mark every test dynamic and disable selection permanently.
`harness_roots` names those artifacts so the walk stops at them. Reflection at
or *below* the test class — a component scan the test triggers, an annotation
naming code by string — is a genuine escape and still fails open.

Test units are **top-level** classes, because that is what JUnit's
`--select-class` addresses: a `@Nested` inner class runs with its enclosing
class, so the group's references are unioned onto the top-level unit. A class
counts as a test carrier if it mentions a Jupiter test annotation *or* inherits
from one that does — the inherited-tests case (`class FooTest extends
AbstractContractTest`) has no annotation of its own, and missing it would skip
a real test.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from .jvmabi import ClassFormatError, ClassRefs, class_refs
from .testimpact import (
    ProviderView,
    TestUnit,
    Unit,
    _vocabulary,
    plan,
    record_index,
)

# Platform code: shipped with the JDK, so it cannot change without the
# toolchain changing (which already invalidates through the tool fingerprint),
# and it is the *vocabulary* reflection is detected in rather than a unit.
# `javax/` is only partly the JDK's — `javax.inject` and friends are
# third-party — so the JDK's roots are listed explicitly instead of by prefix.
# Left out deliberately: `javax/naming/` (JNDI) and `javax/script/`, which load
# code by name; a class touching those should be treated as dynamic.
PLATFORM_PREFIXES: tuple[str, ...] = (
    "java/",
    "jdk/",
    "sun/",
    "com/sun/",
    "javax/accessibility/",
    "javax/annotation/processing/",
    "javax/crypto/",
    "javax/imageio/",
    "javax/lang/model/",
    "javax/management/",
    "javax/net/",
    "javax/print/",
    "javax/security/",
    "javax/smartcardio/",
    "javax/sound/",
    "javax/sql/",
    "javax/swing/",
    "javax/tools/",
    "javax/transaction/",
    "javax/xml/",
    "org/w3c/dom/",
    "org/xml/sax/",
)

# Frameworks whose annotations name code by **string** rather than by a class
# literal. `@MethodSource("com.acme.Providers#args")` makes a test's result
# depend on `Providers` with no reference to it anywhere in the bytecode — a
# genuine hole in the class closure, and one that sits *below* the test rather
# than above it (unlike the harness, see `harness_roots`). Default: fail open;
# recover only when every such string resolves to a class we actually scanned.
STRING_CODE_ANNOTATIONS: frozenset[str] = frozenset(
    {
        "org/junit/jupiter/params/provider/MethodSource",
        "org/junit/jupiter/params/provider/FieldSource",
    }
)

# What a string reference to code must look like: `pkg.Class#member`, or a bare
# member name (which means "in this same class" and is therefore not an escape).
# A string that is not a well-formed Java name cannot be one of these, so
# ignoring it loses nothing.
_STRING_CODE_REF = re.compile(
    r"^(?P<cls>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)#(?P<member>[\w$]+)"
)

# Mentioning any of these annotation types means the class declares tests.
TEST_ANNOTATIONS: frozenset[str] = frozenset(
    {
        "org/junit/jupiter/api/Test",
        "org/junit/jupiter/api/RepeatedTest",
        "org/junit/jupiter/api/TestFactory",
        "org/junit/jupiter/api/TestTemplate",
        "org/junit/jupiter/api/Nested",
        "org/junit/jupiter/params/ParameterizedTest",
    }
)


def class_unit(path: str | Path, root: str | Path) -> Unit:
    """The internal name of a ``.class`` file under a scan ``root``."""
    rel = Path(path).resolve().relative_to(Path(root).resolve())
    return rel.with_suffix("").as_posix()


def fqn(unit: Unit) -> str:
    """A unit's Java source-level name (``com/acme/A$B`` -> ``com.acme.A$B``)."""
    return unit.replace("/", ".")


def top_level(unit: Unit) -> Unit:
    """The enclosing top-level class of a (possibly nested) unit."""
    return unit.split("$", 1)[0]


# A dotted Java name as it appears inside a text resource. Deliberately loose:
# every candidate is checked against the scanned universe, so the membership
# test does the filtering and a false candidate simply never matches.
_NAME_IN_RESOURCE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+")

# Resources that cannot name a class, skipped so a large binary asset is not
# decoded and regex-scanned for nothing.
_UNSCANNED_RESOURCES = (
    ".class", ".jar", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
    ".so", ".dll", ".dylib", ".woff", ".woff2", ".ttf", ".ico", ".mp4", ".bin",
)


@dataclass
class ScanResult:
    classes: dict[Unit, ClassRefs] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)  # entries we could not parse
    resource_names: set[str] = field(default_factory=set)  # dotted names in resources


def scan_classes(
    roots: Iterable[str | Path], *, resources: bool = False
) -> ScanResult:
    """Read every class in ``roots`` — directories of ``.class`` files and/or
    jars. An entry that fails to parse is recorded rather than raised: the core
    turns that into a full run (`ProviderView.uncovered`), which is the safe
    reading of "we do not fully understand this classpath".

    With ``resources=True``, also harvests dotted names out of the non-class
    files, which is how a class gets addressed without any bytecode naming it
    (`META-INF/services`, `spring.factories`, an XML bean definition)."""
    result = ScanResult()
    for root in roots:
        path = Path(root)
        if not path.exists():
            result.unreadable.add(str(path))
        elif path.is_dir():
            for entry in sorted(path.rglob("*")):
                if not entry.is_file():
                    continue
                if entry.suffix == ".class":
                    _absorb(result, str(entry), entry.read_bytes())
                elif resources:
                    _harvest_names(result, entry.name, entry.read_bytes())
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith(".class"):
                        _absorb(result, f"{path}!{name}", zf.read(name))
                    elif resources and not name.endswith("/"):
                        _harvest_names(result, name, zf.read(name))
        else:
            result.unreadable.add(str(path))
    return result


def _harvest_names(result: ScanResult, name: str, data: bytes) -> None:
    if name.lower().endswith(_UNSCANNED_RESOURCES):
        return
    text = data.decode("utf-8", "replace")
    result.resource_names.update(_NAME_IN_RESOURCE.findall(text))


def _absorb(result: ScanResult, label: str, data: bytes) -> None:
    try:
        refs = class_refs(data)
    except (ClassFormatError, struct.error, IndexError, TypeError):
        result.unreadable.add(label)  # unknown format -> the graph has a hole
        return
    if not refs.refs & STRING_CODE_ANNOTATIONS:
        # Only classes that can name code by string need their string pool kept;
        # holding every literal on a real classpath would be gratuitous.
        refs = replace(refs, strings=frozenset())
    result.classes[refs.name] = refs


@dataclass
class JavaProvider:
    """Builds a `ProviderView` from compiled Java.

    ``test_roots`` hold the compiled *tests* (JUnitTestAction's
    ``test_classes_dir``); ``classpath`` is the code under test plus every
    library it runs against — the same classpath the suite executes with. Give
    it the **whole** runtime classpath: a library that is missing here is not
    silently ignored, it makes its referrers dynamic (they always run), so an
    incomplete classpath costs speed, never correctness.

    ``resource_roots`` are directories of test *data* the suite reads
    (`JUnitTestAction.test_resource_dirs`). They hold no code, but a fixture
    file can name a class, so they are scanned for names like any other
    resource. ``runtime_values`` are the values the suite actually runs with —
    system properties, environment values, command-line arguments — because a
    setting like ``-Djavax.xml.parsers.DocumentBuilderFactory=com.acme.MyFactory``
    names a class that platform code will then load. Both feed
    `ProviderView.name_addressed`.

    ``harness_roots`` are the artifacts brix launches the suite *with* — for
    `JUnitTestAction`, its ``console_jar``. This is a **role, not a
    reputation**: the designation attaches to whatever artifact occupies the
    runner slot in a command brix generated, never to a package name (which
    anyone can claim) or a library brix merely recognizes. Left empty, the
    runner is treated as ordinary code and its reflection makes every test
    dynamic — safe, and useless, which is the correct direction to fail.
    """

    test_roots: Sequence[str | Path]
    classpath: Sequence[str | Path] = ()
    harness_roots: Sequence[str | Path] = ()
    resource_roots: Sequence[str | Path] = ()
    runtime_values: Sequence[str] = ()
    platform_prefixes: tuple[str, ...] = PLATFORM_PREFIXES
    test_annotations: frozenset[str] = TEST_ANNOTATIONS

    def view(self) -> ProviderView:
        tests_scan = scan_classes(self.test_roots, resources=True)
        code_scan = scan_classes(self.classpath, resources=True)
        data_scan = scan_classes(self.resource_roots, resources=True)
        harness = frozenset(scan_classes(self.harness_roots).classes)
        scanned: dict[Unit, ClassRefs] = {**code_scan.classes, **tests_scan.classes}
        # Inert code — the JDK and the test harness — is a fixed boundary, not a
        # unit: it cannot change without invalidating the run a level up, and it
        # is the test's caller rather than its dependency.
        universe = {u: r for u, r in scanned.items() if not self._inert(u, harness)}

        code_deps: dict[Unit, frozenset[Unit]] = {}
        dynamic: set[Unit] = set()
        for unit, refs in universe.items():
            deps = set(refs.refs & universe.keys())
            named, unresolved_names = self._string_named_code(refs, universe)
            deps |= named
            code_deps[unit] = frozenset(deps)
            unresolved = {
                r
                for r in refs.refs
                if r not in universe and not self._inert(r, harness)
            }
            if refs.reflective or unresolved or unresolved_names:
                dynamic.add(unit)

        test_classes = {u: r for u, r in tests_scan.classes.items() if u in universe}
        edges_by_unit: dict[TestUnit, set[Unit]] = {}
        for unit in self._test_carriers(test_classes, universe):
            top = top_level(unit)
            if top not in universe or universe[top].abstract:
                continue  # not runnable on its own; its subclasses are the units
            # A nested test runs with its enclosing class, so the group shares
            # one unit. The group's own classes are edges too: editing a test
            # must run it.
            # The closure expands these through `code_deps`, so naming the
            # group's own classes is enough.
            edges_by_unit.setdefault(top, {top}).add(unit)
        test_edges = {t: frozenset(e) for t, e in edges_by_unit.items()}

        # A class named as text — on the classpath, in test data, or in the
        # settings the suite runs with — can be instantiated by a loader that
        # appears in no closure. Enumerating every such loader is unbounded;
        # enumerating where a *name* can come from is not, so we invert it and
        # let the core fail open when one of these changes. The name sources
        # are: our own code (already the closure), configuration (here), and
        # runtime input (also here). What remains beyond them is a name
        # assembled at run time from data brix never sees — see the design
        # note's residual.
        named = {
            n.replace(".", "/")
            for n in (
                tests_scan.resource_names
                | code_scan.resource_names
                | data_scan.resource_names
                | self._runtime_names()
            )
        }
        return ProviderView(
            code_deps=code_deps,
            test_edges=test_edges,
            dynamic_units=frozenset(dynamic),
            uncovered=frozenset(
                tests_scan.unreadable | code_scan.unreadable | data_scan.unreadable
            ),
            name_addressed=frozenset(named & universe.keys()),
            binding_unit="class",
        )

    def _runtime_names(self) -> set[str]:
        """Dotted names in the values the suite runs with. A JVM setting such as
        `-Djava.util.logging.config.class=com.acme.LogSetup` hands platform code
        the name of one of our classes without any file or bytecode saying so."""
        found: set[str] = set()
        for value in self.runtime_values:
            found.update(_NAME_IN_RESOURCE.findall(str(value)))
        return found

    def _inert(self, name: Unit, harness: frozenset[Unit]) -> bool:
        """Code that is a fixed boundary rather than a unit: the JDK, and the
        harness artifacts brix launched the suite with. Neither can change
        without invalidating the run through the tool / console-jar fingerprint
        a level up, and the harness is the test's *caller*, not its
        dependency."""
        return name in harness or name.startswith(self.platform_prefixes)

    def _string_named_code(
        self, refs: ClassRefs, universe: dict[Unit, ClassRefs]
    ) -> tuple[set[Unit], set[str]]:
        """Resolve annotation values that name code by string, e.g.
        `@MethodSource("com.acme.Providers#args")`.

        Returns the units such a string resolves to, and the strings it does
        not. A resolved one becomes a real edge (the test now tracks its data
        provider); an unresolved one makes the class dynamic, because its
        result depends on code we cannot locate. Bare member names mean "this
        same class" and are not an escape."""
        if not refs.refs & STRING_CODE_ANNOTATIONS:
            return set(), set()
        found: set[Unit] = set()
        missing: set[str] = set()
        for text in refs.strings:
            match = _STRING_CODE_REF.match(text)
            if match is None:
                continue  # not a well-formed reference, so it cannot be one
            unit = match.group("cls").replace(".", "/")
            if unit in universe:
                found.add(unit)
            elif not unit.startswith(self.platform_prefixes):
                missing.add(text)
        return found, missing

    def _test_carriers(
        self, tests: dict[Unit, ClassRefs], universe: dict[Unit, ClassRefs]
    ) -> set[Unit]:
        """Classes that declare or inherit JUnit tests.

        Inheritance is closed over the *whole* universe, not just the test
        roots, so a shared abstract contract test living in a library still
        marks its concrete subclasses."""
        declares = {
            unit
            for unit, refs in universe.items()
            if refs.refs & self.test_annotations
        }
        # Push "carries tests" down the subtype graph until it stops growing.
        carriers = set(declares)
        changed = True
        while changed:
            changed = False
            for unit, refs in universe.items():
                if unit not in carriers and refs.supers & carriers:
                    carriers.add(unit)
                    changed = True
        return {u for u in tests if u in carriers and not universe[u].abstract}

    def select_args(self, tests: Iterable[TestUnit]) -> list[str]:
        """JUnit console-launcher flags running exactly ``tests``."""
        args: list[str] = []
        for unit in sorted(tests):
            args += ["--select-class", fqn(unit)]
        return args


# --- the run-action wiring ----------------------------------------------------
#
# Everything above decides *which tests* to run from a snapshot. This turns that
# decision into what a `JUnitTestAction` runs, at build-file evaluation time —
# the same place `load_affected` selects packages. The footprint rule (docs)
# holds throughout: selection changes only the runner's `--select-class` set,
# never what the action fingerprints (still the whole classpath), so a stale
# pass is impossible — any relevant change still re-fires the action.

# The console-launcher flag that runs the whole suite: JUnit scans the classpath
# itself. This is the fail-open command — emitted on a cold build, a full-run
# trigger, or any change we cannot attribute to a unit.
SCAN_ALL: tuple[str, ...] = ("--scan-class-path",)


def changed_units_from_paths(
    paths: Iterable[str],
    source_roots: Iterable[str],
    vocabulary: Iterable[Unit],
) -> frozenset[Unit] | None:
    """Map changed **source paths** to the compiled **units** they define.

    Returns ``None`` — fail open, run everything — if any changed path cannot be
    attributed to a unit (a path under no known source root, or a non-``.java``
    input we cannot relate to a class). A ``.java`` maps to its unit by stripping
    the source root; the result is widened to every inner class of that unit
    present in the vocabulary, so a test that reaches only an inner class is
    never missed. Paths are repo-relative POSIX; roots likewise."""
    roots = [PurePosixPath(str(r).strip("/")) for r in source_roots]
    vocab = frozenset(vocabulary)
    units: set[Unit] = set()
    for p in paths:
        pp = PurePosixPath(str(p))
        rel = None
        for r in roots:
            try:
                rel = pp.relative_to(r)
                break
            except ValueError:
                continue
        if rel is None or rel.suffix != ".java":
            return None  # cannot attribute this change to a unit -> fail open
        base = rel.with_suffix("").as_posix()
        inner = {u for u in vocab if u == base or u.startswith(base + "$")}
        units |= inner or {base}
    return frozenset(units)


def changed_since_last_build(
    metadata, root: str | Path, rel_paths: Iterable[str]
) -> list[str]:
    """The subset of ``rel_paths`` whose current bytes differ from the hash the
    last build recorded — a self-contained change signal that needs no VCS. A
    path with no recorded hash (first build, new file) counts as changed. Keys
    match the incremental layer's own (absolute path string)."""
    root = Path(root)
    changed: list[str] = []
    for rel in rel_paths:
        f = root / rel
        current = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
        if metadata.hash_by_path(str(f)) != current:
            changed.append(rel)
    return changed


@dataclass(frozen=True)
class SuitePlan:
    """The outcome of selecting for one suite: ``select`` is the runner flag
    tuple to hand a ``JUnitTestAction`` (via ``select=" ".join(plan.select)``);
    the rest is for diagnostics/tests."""

    select: tuple[str, ...]
    run_all: bool
    tests: frozenset[TestUnit]
    changed: frozenset[Unit] | None


def plan_suite(
    provider: JavaProvider,
    *,
    index_path: str | Path,
    source_roots: Iterable[str],
    changed_paths: Iterable[str],
    prior_failed: bool = False,
) -> SuitePlan:
    """Select for one suite and persist its index. Reads the provider's view of
    the *last build's* compiled artifacts (which is all that exists at
    evaluation time) and the changed source paths, decides the runner flags, and
    writes the index for next time.

    When nothing maps to a change, the last build's command is reused rather than
    flipped to a full scan — so a no-op build stays a clean replay instead of
    re-running the suite once more just because the command string moved.

    ``known`` (for the new-or-renamed trigger) is taken **live** from the view,
    not from the stored index: the view reads the *last build's* compiled
    artifacts, so its vocabulary already is exactly the set of units that existed
    as of the last build. A brand-new or renamed source has no artifact yet, so
    it is absent from that vocabulary and correctly forces a full run — with no
    warm-up lag, and no way for a persisted list to drift from what is on disk."""
    view = provider.view()
    changed = changed_units_from_paths(changed_paths, source_roots, _vocabulary(view))

    index_path = Path(index_path)
    try:
        stored = json.loads(index_path.read_text())
    except (OSError, ValueError):
        stored = None

    if changed is None:
        select: tuple[str, ...] = SCAN_ALL
        run_all, tests = True, view.all_tests
    else:
        # index=record_index(view) sets known := the live vocabulary (above).
        selection, _ = plan(
            view, changed, index=record_index(view), prior_failed=prior_failed
        )
        run_all, tests = selection.run_all, selection.tests
        if run_all:
            select = SCAN_ALL
        elif not selection.tests:
            # Provably nothing affected (commonly: no source changed). Reuse the
            # last command so an unchanged classpath still replays.
            last = (stored or {}).get("last_select")
            select = tuple(last) if last else SCAN_ALL
        else:
            select = tuple(provider.select_args(selection.tests))

    new_index = record_index(view)
    new_index["last_select"] = list(select)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_index, sort_keys=True, indent=1))
    tmp.replace(index_path)

    return SuitePlan(
        select=select, run_all=run_all, tests=frozenset(tests), changed=changed
    )
