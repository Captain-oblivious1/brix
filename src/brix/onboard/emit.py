"""Emitter — the tool-neutral *backend* of the onboarder.

Reads the onboarding IR (produced by any dumper — see ``brix.onboard.gradle``)
and writes a runnable ``brix_build.py``. It lives at the ``onboard`` level, not
under ``gradle/``, because the IR is tool-neutral: one emitter serves every
future frontend (Maven, CMake, Make).

Scope of this increment — the **native JVM spine**. Per module with a compile
task it emits a native brix compile+jar (``java``/``kotlin`` skin), delegating
classpath resolution back to the source build via ``java.gradle(...)`` (the
sanctioned migration path — declarations stay in the build files; brix owns
compilation and incrementality). Recognized compile/jar/resource tasks map to
native atoms; everything else is inventoried as a bridge candidate for a later
increment.

Some facts a build needs are *semantic* and simply are not in the IR — a task's
declared inputs/outputs cannot reveal a Kotlin module's main class or that it
needs the Spring all-open compiler plugin. The emitter never guesses these: it
emits an explicit ``# TODO`` and records a note, so a human closes the gap
(exactly the onboarder-surfaces-gaps loop). ``emit_build_file`` returns those
notes alongside the source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EmitResult:
    source: str
    #: Human-facing gaps the emitter could not resolve from the IR alone.
    notes: list[str] = field(default_factory=list)
    #: Module paths that got a native spine (":", ":app", ...).
    emitted_modules: list[str] = field(default_factory=list)
    #: Module paths skipped (no compile task).
    skipped_modules: list[str] = field(default_factory=list)


def _slug(module_path: str, name: str) -> str:
    """A filesystem/identifier-safe token for a module (":a:b" -> "a-b")."""
    stripped = module_path.strip(":")
    return stripped.replace(":", "-") if stripped else name


def _pick_dir(dirs: list[str], leaf: str) -> str | None:
    """Prefer the source root whose leaf is ``leaf`` (…/kotlin, …/java)."""
    for d in dirs:
        if d.rstrip("/").rsplit("/", 1)[-1] == leaf:
            return d
    return dirs[0] if dirs else None


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _main_sourceset(module: dict) -> dict | None:
    for ss in module["source_sets"]:
        if ss["name"] == "main":
            return ss
    return None


def _module_language(ir: dict, module_path: str) -> str | None:
    """'kotlin' if the module has a KotlinCompile task, else 'java' if it has a
    JavaCompile, else None (nothing to compile natively)."""
    types = [n["type"] for n in ir["nodes"] if n["module"] == module_path]
    if any(t.endswith("KotlinCompile") for t in types):
        return "kotlin"
    if any(t.endswith("JavaCompile") for t in types):
        return "java"
    return None


# Task types that make up the native spine — mapped natively, not bridged.
_SPINE_SUFFIXES = ("JavaCompile", "KotlinCompile", "Jar", "Test", "ProcessResources")


def _bridge_candidates(ir: dict) -> list[dict]:
    """Unrecognized tasks that declare outputs — the real work a later increment
    would bridge (custom generators etc.), excluding the native spine and the
    no-op/reporting tasks that declare nothing."""
    out = []
    for n in ir["nodes"]:
        if n["recognized"]:
            continue
        if any(n["type"].endswith(s) for s in _SPINE_SUFFIXES):
            continue
        if not n["outputs"]:
            continue
        out.append(n)
    return out


def emit_build_file(
    ir: dict,
    *,
    root_env_var: str = "BRIX_PROJECT_ROOT",
    gradle_env_var: str = "BRIX_GRADLE",
) -> EmitResult:
    """Render a ``brix_build.py`` from the onboarding ``ir``."""
    if ir.get("source_tool") != "gradle":
        # Only the Gradle frontend exists today; the emitter maps its classpath
        # via java.gradle(). Other tools will need their own dep adapter.
        raise NotImplementedError(f"unsupported source_tool: {ir.get('source_tool')!r}")

    root = ir["root"]
    notes: list[str] = []
    emitted: list[str] = []
    skipped: list[str] = []
    needs_java_worker = False
    body: list[str] = []
    aliases: dict[str, str] = {}

    for module in ir["modules"]:
        mpath = module["path"]
        lang = _module_language(ir, mpath)
        if lang is None:
            skipped.append(mpath)
            continue
        ss = _main_sourceset(module)
        if ss is None:
            skipped.append(mpath)
            notes.append(f"{mpath}: has a compile task but no 'main' source set; skipped.")
            continue

        slug = _slug(mpath, module["name"])
        jar = f"build/brix/{slug}.jar"
        classes_dir = f"build/brix/{slug}-classes"
        project_arg = "None" if mpath == ":" else repr(mpath)

        # Source glob: pick the language's source root, relative to the graph root.
        if lang == "kotlin":
            src_root = _pick_dir(ss.get("kotlin_dirs") or ss["java_dirs"], "kotlin")
            glob_suffix = "{name}.kt"
        else:
            src_root = _pick_dir(ss["java_dirs"], "java")
            glob_suffix = "{name}.java"
        if not src_root:
            skipped.append(mpath)
            notes.append(f"{mpath}: no {lang} source directory in the IR; skipped.")
            continue
        src_glob = f"{_rel(src_root, root)}/{glob_suffix}"

        # Resource dir (optional).
        res_dirs = ss.get("resource_dirs") or []
        res_rel = _rel(res_dirs[0], root) if res_dirs else None

        cp = f"cp_{slug.replace('-', '_')}"
        block: list[str] = []
        block.append(f"    # --- module {mpath} ({lang}) ---")
        block.append(
            f"    {cp} = java.gradle(\n"
            f"        ROOT, project={project_arg}, configuration={'compileClasspath'!r},\n"
            f"        gradle=GRADLE, cache_dir={'build/.brix-deps'!r}, cwd=str(ROOT),\n"
            f"    )"
        )

        action = "kotlin.JarAction" if lang == "kotlin" else "java.JarAction"
        value_args = [
            f"source_files=data.glob({src_glob!r})",
            f"output_file=data.file({jar!r})",
            f"classes_dir={classes_dir!r}",
            f"classpath={cp}",
        ]
        if res_rel:
            value_args.append(f"resource_dir={res_rel!r}")
        comment_lines = []
        if lang == "kotlin":
            value_args.append('jvm_target="17"')
            comment_lines.append("# TODO: jvm_target assumed 17 — not derivable from the IR")
            comment_lines.append("# TODO: spring_allopen / Kotlin compiler plugins are not in the IR")
        else:
            value_args.append("javac_worker=worker")
            needs_java_worker = True
        comment_lines.append("# TODO: entry_point (main class) is not derivable from the IR")

        block.append(f"    graph.add(\n        {action}(")
        for a in value_args:
            block.append(f"            {a},")
        for c in comment_lines:
            block.append(f"            {c}")
        block.append("        )\n    )")

        body.append("\n".join(block))
        emitted.append(mpath)
        aliases[slug] = jar

        # Note the semantic gaps for this module.
        if lang == "kotlin":
            notes.append(f"{mpath}: Kotlin main class + compiler plugins (e.g. Spring all-open) not derivable — see TODOs.")
        else:
            notes.append(f"{mpath}: Java main class not derivable — see entry_point TODO.")

    # Bridge inventory (not emitted as actions this increment).
    bridges = _bridge_candidates(ir)
    if bridges:
        by_type: dict[str, int] = {}
        for n in bridges:
            by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        top = sorted(by_type.items(), key=lambda kv: -kv[1])[:8]
        notes.append(
            f"{len(bridges)} non-spine task(s) with outputs were NOT mapped "
            f"(bridge candidates for a later increment); top types: "
            + ", ".join(f"{t.split('.')[-1]}×{c}" for t, c in top)
        )

    source = _render(ir, root, root_env_var, gradle_env_var, needs_java_worker, body, aliases, bridges)
    return EmitResult(source=source, notes=notes, emitted_modules=emitted, skipped_modules=skipped)


def _render(ir, root, root_env_var, gradle_env_var, needs_java_worker, body, aliases, bridges) -> str:
    worker_line = "    worker = java.javac_worker(graph)\n" if needs_java_worker else ""
    bridge_comment = ""
    if bridges:
        lines = "\n".join(f"#   {n['id']}  ({n['type'].split('.')[-1]})" for n in bridges[:20])
        more = "" if len(bridges) <= 20 else f"\n#   ... and {len(bridges) - 20} more"
        bridge_comment = (
            "\n# Non-spine tasks NOT mapped by this increment (bridge candidates):\n"
            f"{lines}{more}\n"
        )
    alias_items = ",\n            ".join(f"{k!r}: {v!r}" for k, v in aliases.items())
    return f'''#!/usr/bin/env python3
"""AUTO-GENERATED by the brix onboarder (brix.onboard.emit) from a {ir['source_tool']} build.

Native JVM spine only: compile+jar per module, classpaths delegated to the
source build via java.gradle(). Review the TODOs below — the IR cannot carry
main classes or compiler-plugin choices. Regenerate rather than hand-editing
where possible; treat this as a starting point, not a frozen artifact.
"""

import os
import sys
from pathlib import Path

# brix must be importable. In-tree: set BRIX_SRC to the repo's src/ directory.
_brix_src = os.environ.get("BRIX_SRC")
if _brix_src:
    sys.path.insert(0, _brix_src)

from brix import Graph, cli, java, kotlin  # noqa: E402

ROOT = Path(os.environ.get({root_env_var!r}, {root!r})).resolve()
GRADLE = os.environ.get({gradle_env_var!r}, str(ROOT / "gradlew"))
{bridge_comment}

def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
{worker_line}
{chr(10).join(body)}
    return graph


if __name__ == "__main__":
    from brix import AtomicBuilder, CacheBuilder, IncrementalBuilder

    def builder():
        return AtomicBuilder(
            CacheBuilder(
                IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
                cache_dir="build/.brix-cache",
            ),
            staging_dir="build/.brix-staging",
        )

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            staging_dir="build/.brix-staging",
            builder_factory=builder,
            aliases={{
            {alias_items}
            }},
        )
    )
'''


def main(argv=None) -> int:
    """CLI: python -m brix.onboard.emit <ir.json> [-o brix_build.py]"""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="brix.onboard.emit",
        description="Emit a brix_build.py from an onboarding IR JSON file.",
    )
    parser.add_argument("ir_json", help="path to the onboarding IR (from a dumper)")
    parser.add_argument("-o", "--out", default=None, help="output path (default: stdout)")
    args = parser.parse_args(argv)

    ir = json.loads(Path(args.ir_json).read_text())
    result = emit_build_file(ir)
    if args.out:
        Path(args.out).write_text(result.source)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(result.source)
    print(
        f"emitted {len(result.emitted_modules)} module(s), "
        f"skipped {len(result.skipped_modules)}",
        file=sys.stderr,
    )
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
