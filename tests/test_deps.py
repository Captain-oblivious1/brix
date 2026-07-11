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
    a = DepSet(("a.jar",), flags=("-Ione",))
    b = DepSet(("b.jar",), flags=("-Itwo",))
    assert (a + b).flags == ("-Ione", "-Itwo")


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
