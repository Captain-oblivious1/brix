"""`brix_build.py help`: introspect the live configuration.

`help` prints what lives inside the `cli.main(...)` call and is otherwise
invisible without reading the build file — which builder layers are wired
(so whether `--stage`/`commit`/`discard` apply), the target aliases, and the
project's build-then-act `commands=`.
"""

from brix import AtomicBuilder, CacheBuilder, IncrementalBuilder, cli

from test_incremental import make_graph, write_workspace


def full_stack(tmp_path):
    def builder():
        return AtomicBuilder(
            CacheBuilder(
                IncrementalBuilder(make_graph, root=tmp_path, metadata_dir="build/.brix"),
                cache_dir="build/.brix-cache",
            ),
            staging_dir="build/.brix-staging",
        )

    return builder


def test_help_names_the_full_stack_outermost_first(tmp_path, capsys):
    assert cli.main(make_graph, root=tmp_path, argv=["help"],
                    builder_factory=full_stack(tmp_path)) == 0
    out = capsys.readouterr().out
    # Order is outermost first, and each layer names its path.
    atomic, cache, incremental = (
        out.index("Atomic"), out.index("Cache"), out.index("Incremental")
    )
    assert atomic < cache < incremental
    assert "build/.brix-staging" in out
    assert "build/.brix-cache" in out
    assert "metadata → build/.brix" in out
    assert "staging is wired" in out


def test_help_default_builder_reports_staging_unavailable(tmp_path, capsys):
    # No builder_factory → the default is a bare IncrementalBuilder.
    assert cli.main(make_graph, root=tmp_path, argv=["help"]) == 0
    out = capsys.readouterr().out
    assert "Incremental" in out
    assert "all-or-nothing" not in out  # no Atomic layer in the stack
    assert "need an AtomicBuilder" in out
    # The staging-only commands are individually flagged as unavailable.
    assert "commit" in out and "not wired here" in out


def test_help_lists_aliases_with_descriptions(tmp_path, capsys):
    aliases = {
        "lib": "build/lib.a",  # bare value: no description
        "app": cli.Alias("build/app.jar", "the runnable service jar"),
    }
    assert cli.main(make_graph, root=tmp_path, argv=["help"], aliases=aliases) == 0
    out = capsys.readouterr().out
    assert "Targets (aliases):" in out
    assert "build/lib.a" in out
    assert "build/app.jar" in out
    assert "the runnable service jar" in out


def test_help_surfaces_command_docstrings(tmp_path, capsys):
    def run(paths):
        """Run each freshly built target."""
        return 0

    assert cli.main(make_graph, root=tmp_path, argv=["help"],
                    commands={"run": run}) == 0
    out = capsys.readouterr().out
    assert "run <targets>" in out
    assert "Run each freshly built target." in out


def test_alias_object_resolves_as_a_build_target(tmp_path):
    """An `Alias` is a drop-in for a bare alias value: `build app` builds
    its target, description notwithstanding."""
    write_workspace(tmp_path, {"a": "hi ", "b": "there"})
    aliases = {"all": cli.Alias("build/all.txt", "the combined output")}
    assert cli.main(make_graph, root=tmp_path, argv=["build", "all", "--silent"],
                    aliases=aliases) == 0
    assert (tmp_path / "build/all.txt").read_text() == "HI THERE"


def test_alias_object_with_list_target(tmp_path, capsys):
    aliases = {"both": cli.Alias(["build/x", "build/y"], "x and y")}
    assert cli.main(make_graph, root=tmp_path, argv=["help"], aliases=aliases) == 0
    out = capsys.readouterr().out
    assert "build/x build/y" in out
    assert "x and y" in out


def test_describe_stack_reads_attributes_without_touching_disk(tmp_path):
    # A cache builder holding a custom store object still describes cleanly.
    class DummyStore:
        pass

    builder = AtomicBuilder(
        CacheBuilder(
            IncrementalBuilder(make_graph, root=tmp_path),
            store=DummyStore(),
        )
    )
    rows = cli._describe_stack(builder)
    names = [name for name, _ in rows]
    assert names == ["Atomic", "Cache", "Incremental"]
    cache_detail = dict(rows)["Cache"]
    assert "custom store" in cache_detail
