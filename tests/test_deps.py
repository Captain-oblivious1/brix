"""Resolved dependency sets (`DepSet`) and the Maven resolver adapters.

The `DepSet` algebra and the adapter plumbing (caching, pom generation, wiring
into the typed actions) are tested hermetically — the actual `mvn` call is
behind `_maven_build_classpath`, which these tests substitute. A live end-to-end
resolution is exercised by the spring-petclinic dogfood, not the unit suite.
"""

import os

import pytest

from brix import DepSet, Graph, java
from brix.data import DataFactory, FileData
from brix.workers import WorkerSpec

data = DataFactory()


# --- DepSet algebra ---


def test_add_merges_ordered_and_dedups_first_occurrence():
    a = DepSet(("x/foo.jar", "x/bar.jar"))
    b = DepSet(("x/bar.jar", "x/baz.jar"))
    assert (a + b).artifacts == ("x/foo.jar", "x/bar.jar", "x/baz.jar")


def test_add_concatenates_flags():
    a = DepSet(("a.jar",), compile_flags=("-Ione",), link_flags=("-lone",))
    b = DepSet(("b.jar",), compile_flags=("-Itwo",), link_flags=("-ltwo",))
    assert (a + b).compile_flags == ("-Ione", "-Itwo")
    assert (a + b).link_flags == ("-lone", "-ltwo")


def test_add_accepts_a_file_atom_or_raw_path():
    a = DepSet(("a.jar",))
    assert (a + FileData("local.jar")).artifacts == ("a.jar", "local.jar")
    assert (a + "raw.jar").artifacts == ("a.jar", "raw.jar")


def test_sub_excludes_by_full_path_or_basename():
    a = DepSet(("m2/foo.jar", "m2/commons-logging-1.2.jar", "m2/bar.jar"))
    assert (a - "*/commons-logging-*.jar").artifacts == ("m2/foo.jar", "m2/bar.jar")
    assert (a - "bar.jar").artifacts == ("m2/foo.jar", "m2/commons-logging-1.2.jar")
    assert (a - ["*foo*", "*bar*"]).artifacts == ("m2/commons-logging-1.2.jar",)


def test_files_are_byte_lens_and_bool():
    a = DepSet(("a.jar", "b.jar"))
    assert [f.path for f in a.files()] == ["a.jar", "b.jar"]
    assert all(f.content_key is None for f in a.files())  # consumer re-lenses
    assert bool(a) and not bool(DepSet())


# --- adapter internals ---


def test_parse_classpath_keeps_only_jars():
    text = os.pathsep.join(["/m2/a.jar", "/m2/b.jar", "/m2/not-a-jar.txt", ""])
    assert java._parse_classpath(text) == ["/m2/a.jar", "/m2/b.jar"]


def test_coords_to_pom_emits_dependencies_and_rejects_bad_coords():
    pom = java._coords_to_pom(["g:a:1.0"])
    assert "<groupId>g</groupId>" in pom and "<artifactId>a</artifactId>" in pom
    assert "<version>1.0</version>" in pom
    with pytest.raises(ValueError):
        java._coords_to_pom(["not-a-coordinate"])


def _fake_resolver(jars):
    def run(mvn, pom, scope, out_file, cwd=None):
        open(out_file, "w").write(os.pathsep.join(jars))
    return run


def test_maven_resolves_and_caches(tmp_path, monkeypatch):
    calls = []

    def run(mvn, pom, scope, out_file, cwd=None):
        calls.append(scope)
        open(out_file, "w").write(os.pathsep.join(["/m2/foo.jar", "/m2/bar.jar"]))

    monkeypatch.setattr(java, "_maven_build_classpath", run)
    ds = java.maven(["g:a:1.0"], cache_dir=str(tmp_path))
    assert ds.artifacts == ("/m2/foo.jar", "/m2/bar.jar")
    # A second identical call is served from the cache — the runner is not hit.
    ds2 = java.maven(["g:a:1.0"], cache_dir=str(tmp_path))
    assert ds2.artifacts == ds.artifacts
    assert calls == ["compile"], "second resolve should have been cached"


def test_changing_coordinates_reresolves(tmp_path, monkeypatch):
    seen = []

    def run(mvn, pom, scope, out_file, cwd=None):
        seen.append(open(pom).read())
        open(out_file, "w").write("/m2/x.jar")

    monkeypatch.setattr(java, "_maven_build_classpath", run)
    java.maven(["g:a:1.0"], cache_dir=str(tmp_path))
    java.maven(["g:a:2.0"], cache_dir=str(tmp_path))  # different version -> new key
    assert len(seen) == 2


def test_pom_resolves_from_a_manifest_file(tmp_path, monkeypatch):
    monkeypatch.setattr(java, "_maven_build_classpath", _fake_resolver(["/m2/z.jar"]))
    pom = tmp_path / "pom.xml"
    pom.write_text("<project/>")
    assert java.pom(pom, cache_dir=str(tmp_path)).artifacts == ("/m2/z.jar",)


def test_scope_is_part_of_the_cache_key(tmp_path, monkeypatch):
    scopes = []

    def run(mvn, pom, scope, out_file, cwd=None):
        scopes.append(scope)
        open(out_file, "w").write("/m2/x.jar")

    monkeypatch.setattr(java, "_maven_build_classpath", run)
    java.maven(["g:a:1.0"], scope="compile", cache_dir=str(tmp_path))
    java.maven(["g:a:1.0"], scope="runtime", cache_dir=str(tmp_path))
    assert scopes == ["compile", "runtime"], "different scopes must not collide"


# --- Gradle adapter ---


def _write_gradle_project(tmp_path, build="dependencies { }", catalog=None):
    (tmp_path / "build.gradle").write_text(build)
    if catalog is not None:
        (tmp_path / "gradle").mkdir(exist_ok=True)
        (tmp_path / "gradle" / "libs.versions.toml").write_text(catalog)


def test_gradle_manifest_material_covers_build_and_catalog(tmp_path):
    _write_gradle_project(tmp_path, build="A", catalog="B")
    material = java._gradle_manifest_material(tmp_path)
    assert "build.gradle\0A" in material
    assert "gradle/libs.versions.toml\0B" in material


def test_gradle_manifest_material_raises_when_no_build_files(tmp_path):
    with pytest.raises(ValueError):
        java._gradle_manifest_material(tmp_path)


def _fake_gradle_resolver(jars):
    def run(gradle, project_dir, configuration, project, out_file, cwd=None, java_installations=()):
        open(out_file, "w").write(os.pathsep.join(jars))
    return run


def test_gradle_resolves_from_a_project_dir_and_caches(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_gradle_project(proj, build="deps v1")
    calls = []

    def run(gradle, project_dir, configuration, project, out_file, cwd=None, java_installations=()):
        calls.append(configuration)
        open(out_file, "w").write(os.pathsep.join(["/g/foo.jar", "/g/bar.jar"]))

    monkeypatch.setattr(java, "_gradle_build_classpath", run)
    cache = str(tmp_path / "cache")
    ds = java.gradle(proj, cache_dir=cache)
    assert ds.artifacts == ("/g/foo.jar", "/g/bar.jar")
    # A second identical call is served from the cache — Gradle is not re-run.
    ds2 = java.gradle(proj, cache_dir=cache)
    assert ds2.artifacts == ds.artifacts
    assert calls == ["compileClasspath"], "second resolve should have been cached"


def test_gradle_accepts_a_build_file_path(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_gradle_project(proj)
    monkeypatch.setattr(
        java, "_gradle_build_classpath", _fake_gradle_resolver(["/g/z.jar"])
    )
    ds = java.gradle(proj / "build.gradle", cache_dir=str(tmp_path / "c"))
    assert ds.artifacts == ("/g/z.jar",)


def test_gradle_configuration_and_project_are_part_of_the_cache_key(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_gradle_project(proj)
    seen = []

    def run(gradle, project_dir, configuration, project, out_file, cwd=None, java_installations=()):
        seen.append((configuration, project))
        open(out_file, "w").write("/g/x.jar")

    monkeypatch.setattr(java, "_gradle_build_classpath", run)
    cache = str(tmp_path / "cache")
    java.gradle(proj, configuration="compileClasspath", cache_dir=cache)
    java.gradle(proj, configuration="runtimeClasspath", cache_dir=cache)  # new conf
    java.gradle(proj, project=":app", cache_dir=cache)  # new project
    assert seen == [
        ("compileClasspath", None),
        ("runtimeClasspath", None),
        ("compileClasspath", ":app"),
    ]


def test_changing_a_build_file_reresolves(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_gradle_project(proj, build="deps v1")
    seen = []

    def run(gradle, project_dir, configuration, project, out_file, cwd=None, java_installations=()):
        seen.append(True)
        open(out_file, "w").write("/g/x.jar")

    monkeypatch.setattr(java, "_gradle_build_classpath", run)
    cache = str(tmp_path / "cache")
    java.gradle(proj, cache_dir=cache)
    _write_gradle_project(proj, build="deps v2")  # edit build.gradle -> new key
    java.gradle(proj, cache_dir=cache)
    assert len(seen) == 2


def test_gradle_build_classpath_builds_task_and_init_script(tmp_path, monkeypatch):
    argv = {}

    def fake_run(cmd, check, cwd):
        argv["cmd"] = cmd
        argv["cwd"] = cwd

    monkeypatch.setattr(java.subprocess, "run", fake_run)
    out = tmp_path / "cp.rawcp"
    java._gradle_build_classpath(
        "./gradlew", str(tmp_path), "runtimeClasspath", "app", str(out), cwd=None
    )
    cmd = argv["cmd"]
    # A leading colon is normalized onto the project path.
    assert ":app:brixPrintClasspath" in cmd
    # Config/out are baked into the script, not passed as -P (which some builds
    # validate and reject) — so no project properties on the command line.
    assert not any(a.startswith("-P") for a in cmd)
    assert argv["cwd"] == str(tmp_path)  # defaults to the project dir
    # The init script was written next to the output and carries the baked-in
    # configuration and output path.
    init = out.with_suffix(".init.gradle")
    script = init.read_text()
    assert "brixPrintClasspath" in script
    assert "runtimeClasspath" in script and str(out) in script
    assert "--init-script" in cmd and str(init) in cmd


def test_gradle_forwards_java_installations_as_toolchain_property(tmp_path, monkeypatch):
    argv = {}
    monkeypatch.setattr(
        java.subprocess, "run", lambda cmd, check, cwd: argv.update(cmd=cmd)
    )
    out = tmp_path / "cp.rawcp"
    java._gradle_build_classpath(
        "gradle", str(tmp_path), "compileClasspath", None, str(out),
        java_installations=("/opt/jdk-17", "/opt/jdk-21"),
    )
    cmd = argv["cmd"]
    # The JDK dirs land in Gradle's own reserved installations property (comma-
    # separated), so a build declaring a toolchain can find a matching JDK.
    assert "-Porg.gradle.java.installations.paths=/opt/jdk-17,/opt/jdk-21" in cmd


def test_gradle_root_project_task_has_no_project_prefix(tmp_path, monkeypatch):
    argv = {}
    monkeypatch.setattr(
        java.subprocess, "run", lambda cmd, check, cwd: argv.update(cmd=cmd)
    )
    out = tmp_path / "cp.rawcp"
    java._gradle_build_classpath(
        "gradle", str(tmp_path), "compileClasspath", None, str(out)
    )
    assert ":brixPrintClasspath" in argv["cmd"]


# --- the typed actions accept a DepSet ---


def test_jar_action_accepts_a_depset_classpath():
    ds = DepSet(("/m2/foo.jar", "/m2/bar.jar"))
    worker = WorkerSpec(name="javac", command_line="{j}", inputs={})
    action = java.JarAction(
        source_files=data.glob("src/{n}.java"),
        output_file=data.file("app.jar"),
        classpath=ds,
        javac_worker=worker,
    )
    assert [a.path for a in action.classpath] == ["/m2/foo.jar", "/m2/bar.jar"]
    # And it flows through to the compile step, ABI-lensed.
    compile_java, _ = action.lower()
    cp = compile_java.inputs["classpath"]
    assert {a.content_key for a in cp.atoms()} == {"jvm-abi"}


def test_junit_and_springboot_accept_a_depset():
    from brix import springboot

    ds = DepSet(("/m2/foo.jar",))
    worker = WorkerSpec(name="javac", command_line="{j}", inputs={})
    jt = java.JUnitTestAction(
        test_sources=data.glob("t/{n}.java"),
        classpath=ds,
        console_jar="lib/console.jar",
        javac_worker=worker,
    )
    assert [a.path for a in jt.classpath] == ["/m2/foo.jar"]

    spec = springboot.RepackagerSpec(helper_jar="h.jar", loader_tools_classpath=())
    boot = springboot.SpringBootJar(
        source_jar=data.file("app.jar"),
        output_file=data.file("app-boot.jar"),
        main_class="App",
        runtime_classpath=ds,
        repackager=spec,
    )
    assert [a.path for a in boot.runtime_classpath] == ["/m2/foo.jar"]


# --- C++: pkg_config adapter + cpp compile/link consumption ---


def test_flags_only_depset_is_truthy():
    # Regression: a pkg-config DepSet has no artifacts, only flags — it must not
    # be falsy, or the cpp lowering drops its flags.
    assert bool(DepSet(link_flags=("-lz",)))
    assert bool(DepSet(compile_flags=("-Ifoo",)))
    assert not bool(DepSet())


def test_pkg_config_splits_cflags_and_libs(monkeypatch):
    from brix import cpp

    def query(pkg_config, flag, names):
        out = {
            "--cflags": ["-I/usr/include/foo", "-DBAR"],
            "--libs": ["-L/usr/lib", "-lfoo"],
        }
        return out[flag]

    monkeypatch.setattr(cpp, "_pkg_config_query", query)
    ds = cpp.pkg_config(["foo"])
    assert ds.compile_flags == ("-I/usr/include/foo", "-DBAR")
    assert ds.link_flags == ("-L/usr/lib", "-lfoo")
    assert ds.artifacts == ()  # system libs are flags, not tracked files


def test_compile_action_adds_dep_compile_flags():
    from brix import cpp

    ds = DepSet(compile_flags=("-I/usr/include/foo",))
    action = cpp.CompileSourceAction(
        source_file=data.glob("src/{n}.cpp"),
        object_file=data.glob("build/{n}.o"),
        deps=ds,
    )
    (expand,) = action.lower()
    child = expand.child
    assert child.inputs["dep_cflags"].text == "-I/usr/include/foo"
    assert "{dep_cflags}" in child.command_line


def test_link_action_puts_dep_libs_and_flags_after_objects():
    from brix import cpp

    ds = DepSet(artifacts=("lib/libfoo.a",), link_flags=("-lz",))
    (link,) = cpp.LinkApplicationAction(
        object_files=data.glob("build/{n}.o"),
        output_file=data.file("build/app"),
        deps=ds,
    ).lower()
    # link order: objects, then dep artifacts, then dep -l flags.
    assert "{objects} {dep_libs} {dep_lflags}" in link.command_line
    assert [a.path for a in link.inputs["dep_libs"].atoms()] == ["lib/libfoo.a"]
    assert link.inputs["dep_lflags"].text == "-lz"
