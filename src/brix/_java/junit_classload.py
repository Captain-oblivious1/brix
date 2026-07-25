#!/usr/bin/env python3
"""Run one JUnit test class and record which ``.class`` files the JVM loaded,
as a Makefile depfile — brix's Java counterpart of ``cc -MMD -MF``.

A test's real dependency set is *the classes it actually loads at run time*.
That is impossible to predict statically without conservative over-approximation
(and a reflection fail-open), but trivial to **observe**: the JVM already lists
every class it loads under ``-Xlog:class+load`` (the modern spelling of
``-verbose:class``), with the file each was loaded *from*. This wrapper runs the
test with that logging to a temp file, keeps the workspace-relative
``.class``/``.jar`` sources, and writes them as a Makefile depfile the
incremental layer already scans — so brix re-runs the test only when a class it
truly used changed, and reflection is handled for free (a reflectively loaded
class is just a loaded class).

Usage::

    junit_classload.py --depfile <path> --test-classes-dir <dir>
        --testclass <dir/pkg/Name.class> --java <java> --console <console.jar>
        --classpath <cp> [--arg <extra console arg>]...

* ``--depfile`` — where to write the Makefile depfile (a declared output).
* ``--test-classes-dir`` / ``--testclass`` — used to derive the FQN for
  ``--select-class`` (``<dir>/com/acme/AdderTest.class`` -> ``com.acme.AdderTest``).
* ``--java`` / ``--console`` — the ``java`` launcher and the
  ``junit-platform-console-standalone`` jar.
* ``--classpath`` — the run classpath (``os.pathsep``-separated), passed to the
  launcher as repeated ``--class-path`` entries.
* ``--arg`` — extra console-launcher arguments (repeatable).

Only sources under the workspace root (the process cwd, where brix runs actions)
are recorded; absolute JDK/toolchain paths are dropped by the scanner exactly as
the C/C++ header scanner drops absolute system prerequisites.
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

# `[.. ][info][class,load] com.acme.Adder source: file:/abs/build/classes/lib/`
# — the JDK class-load line. It gives the class's FQN and the *classpath source*
# it loaded from: a jar (the whole jar) or a directory (the classpath root, NOT
# the individual .class). For a directory we reconstruct the per-class .class
# path from the FQN, which is what gives per-class selection on exploded code.
# Non-file sources ("shared objects file", "jrt:/…" JDK modules) have no path.
_LOAD_RE = re.compile(
    r"class,load\]\s+(?P<fqn>\S+)\s+source:\s+(?:file:)?(?P<src>/\S+)"
)


def _fqn(testclass: str, classes_dir: str) -> str:
    rel = Path(testclass).resolve().relative_to(Path(classes_dir).resolve())
    return rel.with_suffix("").as_posix().replace("/", ".")


def _loaded_sources(classlog: Path, root: Path) -> list[str]:
    """The workspace files backing the classes this run loaded: a jar as itself
    (whole-jar, coarse but sound for libs), a directory-loaded class as its
    reconstructed ``.class`` file (per-class, for your own exploded code)."""
    deps: list[str] = []
    for line in classlog.read_text(errors="replace").splitlines():
        m = _LOAD_RE.search(line)
        if not m:
            continue
        src = m.group("src")
        if src.endswith(".jar"):
            path = Path(src)
        else:  # a classpath directory -> the specific .class within it
            rel_class = m.group("fqn").replace(".", "/") + ".class"
            path = Path(src) / rel_class
        try:
            deps.append(path.resolve().relative_to(root).as_posix())
        except ValueError:
            continue  # outside the workspace (JDK, an out-of-tree jar) -> dropped
    return deps


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--depfile", required=True)
    p.add_argument("--test-classes-dir", required=True)
    p.add_argument("--testclass", required=True)
    p.add_argument("--java", required=True)
    p.add_argument("--console", required=True)
    p.add_argument("--classpath", default="")
    p.add_argument("--arg", action="append", default=[])
    args = p.parse_args(argv)

    fqn = _fqn(args.testclass, args.test_classes_dir)
    cp_flags: list[str] = []
    for entry in args.classpath.split(os.pathsep):
        if entry:
            cp_flags += ["--class-path", entry]

    with tempfile.NamedTemporaryFile(
        suffix=".classlog", delete=False
    ) as tmp:
        classlog = Path(tmp.name)
    try:
        cmd = [
            args.java,
            f"-Xlog:class+load=info:file={classlog}",
            "-jar",
            args.console,
            "execute",
            "--disable-banner",
            "--details=none",
            # No --fail-if-no-tests: one action per *compiled class*, so helper
            # and abstract-base classes legitimately carry no tests and must
            # no-op rather than fail. A real test class still runs its tests.
            "--select-class",
            fqn,
            *cp_flags,
            *args.arg,
        ]
        import subprocess

        proc = subprocess.run(cmd)

        root = Path.cwd().resolve()
        deps = _loaded_sources(classlog, root) if classlog.is_file() else []
        # The test's own compiled class is always a dependency (editing the test
        # must re-run it) even if class-load logging somehow missed it.
        try:
            deps.append(Path(args.testclass).resolve().relative_to(root).as_posix())
        except ValueError:
            pass
        body = " ".join(sorted(set(d.replace(" ", "\\ ") for d in deps)))
        out = Path(args.depfile)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"testrun: {body}\n")
        return proc.returncode
    finally:
        classlog.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
