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
    #: Node ids emitted as bridges (delegated back to the source tool).
    bridged_nodes: list[str] = field(default_factory=list)


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

# Task types that mutate the developer environment or IDE metadata rather than
# produce build artifacts — never bridged (running them as build steps is wrong),
# only counted. Everything else with declared, in-root outputs is a real product.
_BRIDGE_TYPE_BLOCKLIST = (
    "Wrapper",
    "UpdateDaemonJvm",
    "GenerateEclipseProject",
    "GenerateEclipseClasspath",
    "GenerateEclipseJdt",
    "GenerateIdeaModule",
    "GenerateIdeaProject",
    "GenerateIdeaWorkspace",
)


def _in_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _bridge_candidates(ir: dict) -> list[dict]:
    """Unrecognized tasks that produce real, in-root build artifacts — the work a
    bridge delegates back to the source tool. Excludes the native spine, no-op /
    reporting tasks that declare nothing, environment/IDE tasks (blocklist), and
    tasks whose only outputs live outside the project (nothing brix can capture)."""
    root = ir["root"]
    out = []
    for n in ir["nodes"]:
        if n["recognized"]:
            continue
        if any(n["type"].endswith(s) for s in _SPINE_SUFFIXES):
            continue
        if any(n["type"].endswith(s) for s in _BRIDGE_TYPE_BLOCKLIST):
            continue
        if not any(_in_root(o, root) for o in n["outputs"]):
            continue
        out.append(n)
    return out


def _module_by_path(ir: dict) -> dict:
    return {m["path"]: m for m in ir["modules"]}


def _bridge_input_globs(module: dict, root: str) -> list[str]:
    """Coarse, cycle-free over-approximation of what a module's task reads: every
    declared source/resource directory (never the build-output dir). Over-broad
    is safe — it re-invokes the source tool more often, never serves stale. The
    remaining hole (a generator reading a *sibling* module) is the same
    cross-module wiring gap tracked for a later increment."""
    dirs: list[str] = []
    seen = set()
    for ss in module["source_sets"]:
        for d in ss.get("java_dirs", []) + ss.get("kotlin_dirs", []) + ss.get("resource_dirs", []):
            rel = _rel(d, root)
            if rel not in seen:
                seen.add(rel)
                dirs.append(rel)
    return dirs


def _output_exprs(node: dict, root: str) -> list[tuple[str, str]]:
    """(key, code) pairs for a node's in-root outputs. A path that looks like a
    file (a dotted last segment) is a FileData; a directory is a FileData plus a
    recursive glob of its produced files — the idiom JavaCompile uses to capture
    class files it cannot enumerate in advance."""
    exprs: list[tuple[str, str]] = []
    i = 0
    for out in node["outputs"]:
        if not _in_root(out, root):
            continue
        rel = _rel(out, root)
        last = rel.rsplit("/", 1)[-1]
        exprs.append((f"out{i}", f"data.file({rel!r})"))
        if "." not in last:  # a directory of produced files
            exprs.append((f"out{i}_files", f"data.glob({rel + '/{name}'!r})"))
        i += 1
    return exprs


def _emit_bridge_block(node: dict, module: dict, root: str) -> str:
    """A cached ExternalProcessAction that reproduces one non-spine task by
    invoking the source tool for exactly that target. brix caches its outputs
    keyed by the coarse module inputs; a down/absent change just re-invokes the
    tool, which no-ops if nothing changed."""
    target = node["bridge"]["target"]
    cmd = "{gradle} --console=plain -q " + target
    out: list[str] = []
    out.append(f"    # bridge {node['id']}  ({node['type'].split('.')[-1]})")
    out.append("    graph.add(")
    out.append("        ExternalProcessAction(")
    out.append(f"            name={('bridge ' + node['id'])!r},")
    out.append(f"            command_line={cmd!r},")
    out.append("            inputs={")
    out.append('                "gradle": data.tool(GRADLE),')
    bf = module.get("build_file")
    if bf and _in_root(bf, root):
        out.append(f'                "buildfile": data.file({_rel(bf, root)!r}),')
    src_dirs = _bridge_input_globs(module, root)
    if src_dirs:
        for i, d in enumerate(src_dirs):
            out.append(f'                "src{i}": data.glob({(d + "/{name}")!r}),')
    else:
        # No declared source dirs (e.g. an aggregator module): watch the whole
        # module tree minus its build-output dir (avoids an output→input cycle).
        projrel = _rel(module["project_dir"], root)
        pat = "{name}" if projrel == "." else f"{projrel}/{{name}}"
        excl = "build/*" if projrel == "." else f"{projrel}/build/*"
        out.append(f'                "tree": data.glob({pat!r}) - [{excl!r}],')
    out.append("            },")
    out.append("            outputs={")
    for key, code in _output_exprs(node, root):
        out.append(f"                {key!r}: {code},")
    out.append("            },")
    out.append("        )")
    out.append("    )")
    return "\n".join(out)


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

    # Bridges: delegate non-spine build products back to the source tool.
    modules_by_path = _module_by_path(ir)
    bridge_body: list[str] = []
    bridged: list[str] = []
    for node in _bridge_candidates(ir):
        module = modules_by_path.get(node["module"])
        if module is None:
            continue
        bridge_body.append(_emit_bridge_block(node, module, root))
        bridged.append(node["id"])
    if bridged:
        by_type: dict[str, int] = {}
        bridged_set = set(bridged)
        for node in _bridge_candidates(ir):
            if node["id"] in bridged_set:
                by_type[node["type"]] = by_type.get(node["type"], 0) + 1
        top = sorted(by_type.items(), key=lambda kv: -kv[1])[:8]
        notes.append(
            f"{len(bridged)} non-spine task(s) bridged back to gradle (cached by "
            f"coarse per-module inputs; a generator reading a sibling module is a "
            f"known cross-module gap); top types: "
            + ", ".join(f"{t.split('.')[-1]}×{c}" for t, c in top)
        )

    source = _render(
        ir, root, root_env_var, gradle_env_var, needs_java_worker, body, bridge_body, aliases
    )
    return EmitResult(
        source=source,
        notes=notes,
        emitted_modules=emitted,
        skipped_modules=skipped,
        bridged_nodes=bridged,
    )


def _render(ir, root, root_env_var, gradle_env_var, needs_java_worker, body, bridge_body, aliases) -> str:
    worker_line = "    worker = java.javac_worker(graph)\n" if needs_java_worker else ""
    epa_import = ", ExternalProcessAction" if bridge_body else ""
    spine = chr(10).join(body) if body else "    pass  # no compilable module found"
    bridge_section = ""
    if bridge_body:
        bridge_section = (
            "\n    # --- bridged tasks (delegated to the source build) ---\n"
            + "\n".join(bridge_body)
        )
    alias_items = ",\n            ".join(f"{k!r}: {v!r}" for k, v in aliases.items())
    return f'''#!/usr/bin/env python3
"""AUTO-GENERATED by the brix onboarder (brix.onboard.emit) from a {ir['source_tool']} build.

Native JVM spine (compile+jar per module, classpaths delegated via java.gradle())
plus bridged non-spine tasks (each re-invokes the source build for one target,
its outputs cached by brix). Review the TODOs — the IR cannot carry main classes
or compiler-plugin choices. Regenerate rather than hand-editing where possible;
treat this as a starting point, not a frozen artifact.
"""

import os
import sys
from pathlib import Path

# brix must be importable. In-tree: set BRIX_SRC to the repo's src/ directory.
_brix_src = os.environ.get("BRIX_SRC")
if _brix_src:
    sys.path.insert(0, _brix_src)

from brix import Graph, cli, java, kotlin{epa_import}  # noqa: E402

ROOT = Path(os.environ.get({root_env_var!r}, {root!r})).resolve()
GRADLE = os.environ.get({gradle_env_var!r}, str(ROOT / "gradlew"))


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
{worker_line}
{spine}
{bridge_section}
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
