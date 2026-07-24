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

import re
import struct
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .jvmabi import ClassFormatError, ClassRefs, class_refs
from .testimpact import ProviderView, TestUnit, Unit

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


@dataclass
class ScanResult:
    classes: dict[Unit, ClassRefs] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)  # entries we could not parse


def scan_classes(roots: Iterable[str | Path]) -> ScanResult:
    """Read every class in ``roots`` — directories of ``.class`` files and/or
    jars. An entry that fails to parse is recorded rather than raised: the core
    turns that into a full run (`ProviderView.uncovered`), which is the safe
    reading of "we do not fully understand this classpath"."""
    result = ScanResult()
    for root in roots:
        path = Path(root)
        if not path.exists():
            result.unreadable.add(str(path))
        elif path.is_dir():
            for class_file in sorted(path.rglob("*.class")):
                _absorb(result, str(class_file), class_file.read_bytes())
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith(".class"):
                        _absorb(result, f"{path}!{name}", zf.read(name))
        else:
            result.unreadable.add(str(path))
    return result


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
    platform_prefixes: tuple[str, ...] = PLATFORM_PREFIXES
    test_annotations: frozenset[str] = TEST_ANNOTATIONS

    def view(self) -> ProviderView:
        tests_scan = scan_classes(self.test_roots)
        code_scan = scan_classes(self.classpath)
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

        return ProviderView(
            code_deps=code_deps,
            test_edges=test_edges,
            dynamic_units=frozenset(dynamic),
            uncovered=frozenset(tests_scan.unreadable | code_scan.unreadable),
            binding_unit="class",
        )

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
