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
import re
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


# The core `spring-boot-<version>.jar` artifact (not -starter-*/-autoconfigure-*):
# its presence on the classpath both flags a Boot app and dates its loader-tools.
_SPRING_BOOT_JAR = re.compile(r"^spring-boot-(\d[^/]*)\.jar$")


def _is_springboot(ir: dict, module_path: str) -> bool:
    """True if the module runs the Spring Boot plugin — detected by its `bootJar`
    task. Such a module gets a repackaged executable jar (see `springboot`), not
    just the thin spine jar."""
    for n in ir["nodes"]:
        if n["module"] != module_path:
            continue
        if n["task_name"] == "bootJar" or n["type"].endswith("BootJar"):
            return True
    return False


def _spring_boot_version(module: dict) -> str | None:
    """The Spring Boot version, read off the resolved classpath's core
    `spring-boot-<version>.jar` — so loader-tools can be resolved at the matching
    version without a human supplying it. None if not on the classpath."""
    for files in module.get("dependencies", {}).values():
        for p in files:
            m = _SPRING_BOOT_JAR.match(p.rsplit("/", 1)[-1])
            if m:
                return m.group(1)
    return None


def _main_class_const(slug: str) -> str:
    return "MAIN_CLASS_" + re.sub(r"[^0-9A-Za-z]", "_", slug).upper()


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


def _render_module_block(
    plan: dict, plan_by_path: dict, emitted_paths: set, root: str, notes: list
) -> tuple[str, bool]:
    """One module's native spine: a Gradle-delegated classpath and a compile+jar,
    with any brix-built sibling jars wired onto the classpath (and Gradle's stale
    copies of those siblings dropped). Returns (source, uses_java_worker)."""
    mpath, lang, slug = plan["mpath"], plan["lang"], plan["slug"]
    cp = f"cp_{slug.replace('-', '_')}"
    project_arg = "None" if mpath == ":" else repr(mpath)
    worker_used = False
    out: list[str] = [f"    # --- module {mpath} ({lang}) ---"]
    if plan["toolchain"]:
        out.append(
            f"    # This build declares a Java {plan['toolchain']} toolchain. Point "
            f"BRIX_JAVA_INSTALLATIONS at a\n"
            f"    # matching JDK dir if Gradle can't auto-detect one (else "
            f"classpath resolution fails)."
        )
        notes.append(
            f"{mpath}: requires a Java {plan['toolchain']} toolchain — set "
            f"BRIX_JAVA_INSTALLATIONS to a matching JDK if not auto-detected."
        )

    # Cross-module wiring: for each dependency brix also builds, use brix's jar
    # (ABI-lensed by the skin) and exclude Gradle's own build output for it. Skip
    # self (Gradle can report a module as its own project-dep) and dedupe.
    seen_dep: set = set()
    sib_paths = [
        d for d in plan["project_deps"]
        if d in emitted_paths and d != mpath and not (d in seen_dep or seen_dep.add(d))
    ]
    excludes = [
        f"*/{_rel(plan_by_path[d]['module']['project_dir'], root)}/build/*"
        for d in sib_paths
    ]
    sib_jar_exprs = [f"data.file({plan_by_path[d]['jar']!r})" for d in sib_paths]

    if excludes:
        out.append(
            "    # Sibling modules are built by brix; drop Gradle's copies of their "
            "outputs\n    # from the resolved classpath and depend on brix's jars instead."
        )
    gradle_call = (
        f"    {cp} = java.gradle(\n"
        f"        ROOT, project={project_arg}, configuration='compileClasspath',\n"
        f"        gradle=GRADLE, cache_dir='build/.brix-deps', cwd=str(ROOT),\n"
        f"        java_installations=JAVA_INSTALLATIONS,\n"
        f"    )"
    )
    if excludes:
        gradle_call += f" - {excludes!r}"
    out.append(gradle_call)

    classpath_expr = cp + "".join(f" + {e}" for e in sib_jar_exprs)

    action = "kotlin.JarAction" if lang == "kotlin" else "java.JarAction"
    value_args = [
        f"source_files=data.glob({plan['src_glob']!r})",
        f"output_file=data.file({plan['jar']!r})",
        f"classes_dir={plan['classes_dir']!r}",
        f"classpath={classpath_expr}",
    ]
    if plan["res_rel"]:
        value_args.append(f"resource_dir={plan['res_rel']!r}")
    comment_lines: list[str] = []
    if lang == "kotlin":
        value_args.append('jvm_target="17"')
        comment_lines.append("# TODO: jvm_target assumed 17 — not derivable from the IR")
        comment_lines.append("# TODO: spring_allopen / Kotlin compiler plugins are not in the IR")
    else:
        value_args.append("javac_worker=worker")
        worker_used = True
    if not plan["springboot"]:
        # A Boot module's main class rides its fat jar (below), not the thin jar.
        comment_lines.append("# TODO: entry_point (main class) is not derivable from the IR")

    out.append(f"    graph.add(\n        {action}(")
    out += [f"            {a}," for a in value_args]
    out += [f"            {c}" for c in comment_lines]
    out.append("        )\n    )")

    if lang == "kotlin":
        notes.append(f"{mpath}: Kotlin main class + compiler plugins (e.g. Spring all-open) not derivable — see TODOs.")
    else:
        notes.append(f"{mpath}: Java main class not derivable — see entry_point TODO.")
    if sib_paths:
        notes.append(
            f"{mpath}: wired {len(sib_paths)} brix-built sibling jar(s) onto the "
            f"compile classpath ({', '.join(sib_paths)})."
        )
    if plan["springboot"]:
        out.append(_render_springboot_block(plan, project_arg, notes))
    return "\n".join(out), worker_used


def _render_springboot_block(plan: dict, project_arg: str, notes: list) -> str:
    """Repackage a Boot module's thin jar into an executable fat jar so `java -jar`
    alone boots it. loader-tools is resolved (via Maven) at the version read off
    the classpath; the runtime classpath comes from Gradle; the main class is the
    one human-supplied fact (a `@SpringBootApplication` is not in the IR)."""
    slug = plan["slug"]
    mpath = plan["mpath"]
    tag = slug.replace("-", "_")
    lt, rp, rtcp = f"loader_tools_{tag}", f"repackager_{tag}", f"rt_{tag}"
    main_const = _main_class_const(slug)
    ver = plan["boot_version"]
    coord = (
        f"org.springframework.boot:spring-boot-loader-tools:{ver}"
        if ver
        else "org.springframework.boot:spring-boot-loader-tools:TODO_VERSION"
    )
    out = [
        "",
        f"    # --- Spring Boot executable jar for {mpath} ---",
        "    # Detected the Spring Boot plugin (a bootJar task): repackage the thin",
        "    # jar so `java -jar` alone boots it (deps nest under BOOT-INF/lib).",
    ]
    if not ver:
        out.append("    # TODO: could not derive the Spring Boot version from the classpath;")
        out.append("    #       replace TODO_VERSION with the loader-tools version to use.")
        notes.append(f"{mpath}: Spring Boot app — could not derive loader-tools version; fill TODO_VERSION.")
    out += [
        f"    {lt} = java.maven(",
        f"        [{coord!r}],",
        "        scope='runtime', mvn=MVN, cache_dir='build/.brix-deps', cwd=str(ROOT),",
        "    )",
        f"    {rp} = springboot.boot_repackager(graph, loader_tools_classpath=list({lt}.artifacts))",
        f"    {rtcp} = java.gradle(",
        f"        ROOT, project={project_arg}, configuration='runtimeClasspath',",
        "        gradle=GRADLE, cache_dir='build/.brix-deps', cwd=str(ROOT),",
        "        java_installations=JAVA_INSTALLATIONS,",
        "    )",
        "    graph.add(",
        "        springboot.SpringBootJar(",
        f"            source_jar=data.file({plan['jar']!r}),",
        f"            output_file=data.file({f'build/brix/{slug}-boot.jar'!r}),",
        f"            main_class={main_const},",
        f"            runtime_classpath={rtcp},",
        f"            repackager={rp},",
        "        )",
        "    )",
    ]
    notes.append(
        f"{mpath}: emitted a Spring Boot fat jar (alias '{slug}-boot'); set "
        f"{main_const} to the @SpringBootApplication class."
    )
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

    # -- pass 1: decide which modules get a native spine, gathering the facts each
    # block needs. Done first so cross-module wiring can reference any sibling that
    # will be built (its jar path, language) regardless of module order.
    plans: list[dict] = []
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
        slug = _slug(mpath, module["name"])
        res_dirs = ss.get("resource_dirs") or []
        plans.append({
            "mpath": mpath,
            "module": module,
            "lang": lang,
            "slug": slug,
            "jar": f"build/brix/{slug}.jar",
            "classes_dir": f"build/brix/{slug}-classes",
            "src_glob": f"{_rel(src_root, root)}/{glob_suffix}",
            "res_rel": _rel(res_dirs[0], root) if res_dirs else None,
            "toolchain": module.get("java_toolchain"),
            "project_deps": list(module.get("project_deps", [])),
            "springboot": _is_springboot(ir, mpath),
            "boot_version": _spring_boot_version(module),
        })

    plan_by_path = {p["mpath"]: p for p in plans}
    emitted_paths = set(plan_by_path)
    has_cross_module = any(
        any(d in emitted_paths for d in p["project_deps"]) for p in plans
    )
    boot_consts = [
        (_main_class_const(p["slug"]), p["mpath"]) for p in plans if p["springboot"]
    ]

    # -- pass 2: render each module's spine (+ sibling wiring / Boot fat jar).
    for p in plans:
        block, worker_used = _render_module_block(
            p, plan_by_path, emitted_paths, root, notes
        )
        needs_java_worker = needs_java_worker or worker_used
        body.append(block)
        emitted.append(p["mpath"])
        aliases[p["slug"]] = p["jar"]
        if p["springboot"]:
            aliases[f"{p['slug']}-boot"] = f"build/brix/{p['slug']}-boot.jar"

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
        ir, root, root_env_var, gradle_env_var, needs_java_worker, body, bridge_body,
        aliases, has_cross_module=has_cross_module, boot_consts=boot_consts,
    )
    return EmitResult(
        source=source,
        notes=notes,
        emitted_modules=emitted,
        skipped_modules=skipped,
        bridged_nodes=bridged,
    )


def _render(
    ir, root, root_env_var, gradle_env_var, needs_java_worker, body, bridge_body,
    aliases, *, has_cross_module=False, boot_consts=(),
) -> str:
    worker_line = "    worker = java.javac_worker(graph)\n" if needs_java_worker else ""
    epa_import = ", ExternalProcessAction" if bridge_body else ""
    sb_import = "\nfrom brix import springboot  # noqa: E402" if boot_consts else ""
    spine = chr(10).join(body) if body else "    pass  # no compilable module found"
    bridge_section = ""
    if bridge_body:
        bridge_section = (
            "\n    # --- bridged tasks (delegated to the source build) ---\n"
            + "\n".join(bridge_body)
        )
    # Spring Boot needs a Maven launcher (for loader-tools) and a human-supplied
    # main class per Boot module — surfaced as editable module-level constants.
    boot_config = ""
    if boot_consts:
        lines = [
            "",
            "# Maven launcher used only to resolve spring-boot-loader-tools for the",
            "# executable-jar repackager (the project's own deps still come from Gradle).",
            'MVN = os.environ.get("BRIX_MVN", "mvn")',
            "",
            "# TODO: set each to its module's @SpringBootApplication main class.",
        ]
        for const, mpath in boot_consts:
            lines.append(f'{const} = os.environ.get({const!r}, "TODO.SetMainClass")  # {mpath}')
        boot_config = "\n".join(lines) + "\n"
    alias_items = ",\n            ".join(f"{k!r}: {v!r}" for k, v in aliases.items())

    # Cross-module builds must NOT use the atomic overlay: javac/kotlinc resolve
    # a sibling jar via -classpath from the real filesystem, and atomic staging
    # would hide a freshly built sibling from the module that consumes it (the
    # parked "staged-overlay invisible to foreign resolvers" limitation). Write
    # outputs in place via a plain IncrementalBuilder instead.
    if has_cross_module:
        builder_note = (
            "    # Cross-module build: outputs are written in place (no atomic overlay)\n"
            "    # so each module's javac/kotlinc can see sibling jars on -classpath.\n"
        )
        builder_body = (
            "        return CacheBuilder(\n"
            "            IncrementalBuilder(create_graph, root=ROOT, metadata_dir=\"build/.brix\"),\n"
            "            cache_dir=\"build/.brix-cache\",\n"
            "        )"
        )
        builder_imports = "from brix import CacheBuilder, IncrementalBuilder"
    else:
        builder_note = ""
        builder_body = (
            "        return AtomicBuilder(\n"
            "            CacheBuilder(\n"
            "                IncrementalBuilder(create_graph, root=ROOT, metadata_dir=\"build/.brix\"),\n"
            "                cache_dir=\"build/.brix-cache\",\n"
            "            ),\n"
            "            staging_dir=\"build/.brix-staging\",\n"
            "        )"
        )
        builder_imports = "from brix import AtomicBuilder, CacheBuilder, IncrementalBuilder"
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

from brix import Graph, cli, java, kotlin{epa_import}  # noqa: E402{sb_import}

ROOT = Path(os.environ.get({root_env_var!r}, {root!r})).resolve()
GRADLE = os.environ.get({gradle_env_var!r}, str(ROOT / "gradlew"))

# Extra JDK directories Gradle should consider for toolchain matching (a build
# may require a Java version this machine does not auto-detect). Set per machine;
# empty leaves Gradle's own auto-detection untouched. os.pathsep-separated.
JAVA_INSTALLATIONS = [
    p for p in os.environ.get("BRIX_JAVA_INSTALLATIONS", "").split(os.pathsep) if p
]
{boot_config}

def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
{worker_line}
{spine}
{bridge_section}
    return graph


if __name__ == "__main__":
    {builder_imports}

    def builder():
{builder_note}{builder_body}

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
