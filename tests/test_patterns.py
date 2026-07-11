from brix.patterns import PathPattern, overlaps


def test_concrete_pattern():
    p = PathPattern("src/main.cpp")
    assert p.is_concrete
    assert p.match("src/main.cpp") == {}
    assert p.match("src/other.cpp") is None


def test_binding_extraction():
    p = PathPattern("src/{name}.cpp")
    assert p.binding_names == ("name",)
    assert p.match("src/foo.cpp") == {"name": "foo"}
    # Bare {name} is recursive by default (like make's %): matches across /.
    assert p.match("src/nested/foo.cpp") == {"name": "nested/foo"}
    assert p.match("src/foo.h") is None


def test_single_segment_constraint():
    p = PathPattern("src/{name:*}.cpp")
    assert p.binding_names == ("name",)
    assert p.match("src/foo.cpp") == {"name": "foo"}
    assert p.match("src/nested/foo.cpp") is None  # {name:*} is one segment


def test_multiple_bindings():
    p = PathPattern("{dir}/{name}.o")
    assert p.match("obj/foo.o") == {"dir": "obj", "name": "foo"}


def test_repeated_binding_must_agree():
    p = PathPattern("src/{name}/{name}.cpp")
    assert p.match("src/foo/foo.cpp") == {"name": "foo"}
    assert p.match("src/foo/bar.cpp") is None


def test_substitute():
    p = PathPattern("build/obj/{name}.o")
    assert p.substitute({"name": "foo"}) == "build/obj/foo.o"
    assert p.substitute({"other": "x"}) == "build/obj/{name}.o"


def test_literal_prefix_dir():
    assert PathPattern("build/obj/{name}.o").literal_prefix_dir == "build/obj"
    assert PathPattern("{name}.o").literal_prefix_dir == ""
    assert PathPattern("src/main.cpp").literal_prefix_dir == "src"


def test_find(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/foo.cpp").write_text("f")
    (tmp_path / "src/bar.cpp").write_text("b")
    (tmp_path / "src/baz.h").write_text("h")
    found = PathPattern("src/{name}.cpp").find(tmp_path)
    assert found == [
        ("src/bar.cpp", {"name": "bar"}),
        ("src/foo.cpp", {"name": "foo"}),
    ]


def test_find_concrete(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert PathPattern("a.txt").find(tmp_path) == [("a.txt", {})]
    assert PathPattern("b.txt").find(tmp_path) == []


def test_recursive_binding_matches_across_segments():
    # Bare {rel} is recursive by default; {rel:**} is the explicit form.
    for template in ("build/classes/{rel}.class", "build/classes/{rel:**}.class"):
        p = PathPattern(template)
        assert p.binding_names == ("rel",)
        assert not p.is_concrete
        assert p.literal_prefix_dir == "build/classes"
        assert p.match("build/classes/App.class") == {"rel": "App"}
        assert p.match("build/classes/com/foo/Bar.class") == {"rel": "com/foo/Bar"}
        assert p.match("build/classes/x.txt") is None  # wrong extension
        assert p.match("other/App.class") is None  # wrong prefix
        assert p.substitute({"rel": "com/foo/Bar"}) == "build/classes/com/foo/Bar.class"
    # {name:*} restricts to a single segment.
    assert PathPattern("src/{name:*}.java").match("src/a/b.java") is None


def test_recursive_find_walks_nested(tmp_path):
    (tmp_path / "build/classes/com/foo").mkdir(parents=True)
    (tmp_path / "build/classes/App.class").write_text("a")
    (tmp_path / "build/classes/com/foo/Bar.class").write_text("b")
    (tmp_path / "build/classes/com/foo/notes.txt").write_text("n")  # excluded
    found = PathPattern("build/classes/{rel}.class").find(tmp_path)
    assert found == [
        ("build/classes/App.class", {"rel": "App"}),
        ("build/classes/com/foo/Bar.class", {"rel": "com/foo/Bar"}),
    ]


def test_exclude_filters_find(tmp_path):
    (tmp_path / "src/db").mkdir(parents=True)
    (tmp_path / "src/a.cc").write_text("a")
    (tmp_path / "src/a_test.cc").write_text("t")
    (tmp_path / "src/db/b.cc").write_text("b")
    (tmp_path / "src/db/b_test.cc").write_text("t")
    p = PathPattern("src/{name}.cc", exclude=("*_test.cc",))
    # fnmatch `*` crosses `/`: the nested test file is dropped too.
    assert p.find(tmp_path) == [
        ("src/a.cc", {"name": "a"}),
        ("src/db/b.cc", {"name": "db/b"}),
    ]


def test_exclude_exact_path_and_question_mark(tmp_path):
    (tmp_path / "u").mkdir()
    for name in ("env.cc", "env_posix.cc", "env_windows.cc", "x1.cc", "x22.cc"):
        (tmp_path / "u" / name).write_text("s")
    found = PathPattern("u/{n}.cc", exclude=("u/env_windows.cc", "u/x?.cc")).find(tmp_path)
    assert [f for f, _ in found] == ["u/env.cc", "u/env_posix.cc", "u/x22.cc"]


def test_exclude_applies_to_concrete_template(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert PathPattern("a.txt", exclude=("*.txt",)).find(tmp_path) == []
    assert PathPattern("a.txt", exclude=("*.log",)).find(tmp_path) == [("a.txt", {})]


def test_exclude_does_not_affect_match_or_overlap():
    # Membership only: `match` (used for binding extraction and edge
    # inference) and `overlaps` ignore excludes — conservative by design.
    p = PathPattern("src/{name}.cc", exclude=("*_test.cc",))
    assert p.match("src/a_test.cc") == {"name": "a_test"}
    assert overlaps(p, PathPattern("src/foo_test.cc"))


def test_overlaps_concrete():
    assert overlaps(PathPattern("a/b.c"), PathPattern("a/b.c"))
    assert not overlaps(PathPattern("a/b.c"), PathPattern("a/x.c"))


def test_overlaps_pattern_vs_concrete():
    assert overlaps(PathPattern("src/{name}.cpp"), PathPattern("src/foo.cpp"))
    assert not overlaps(PathPattern("src/{name}.cpp"), PathPattern("src/foo.h"))


def test_overlaps_pattern_vs_pattern():
    assert overlaps(PathPattern("build/obj/{name}.o"), PathPattern("build/obj/{name}.o"))
    assert overlaps(PathPattern("build/{a}.o"), PathPattern("build/{b}.o"))
    assert not overlaps(PathPattern("build/obj/{name}.o"), PathPattern("src/{name}.cpp"))


def test_overlaps_shared_binding_is_rigid():
    # A shared binding holds the same value on both sides, so a recursive
    # {video} can no longer be split two ways: out/{video}.mpg vs
    # out/{video}/{scene}.mpg collide as out/a/b.mpg without sharing
    # (video="a/b" vs video="a", scene="b") but never with it.
    a = PathPattern("out/{video}.mpg")
    b = PathPattern("out/{video}/{scene}.mpg")
    assert overlaps(a, b)
    assert not overlaps(a, b, shared=frozenset({"video"}))
    # The real dependency survives: identical tails still overlap.
    raw = PathPattern("out/{video}.raw.mpg")
    assert overlaps(raw, raw, shared=frozenset({"video"}))
    # Same-value equality also kills the shortest-stem shape.
    assert overlaps(PathPattern("{video}.mpg"), PathPattern("{video}.raw.mpg"))
    assert not overlaps(
        PathPattern("{video}.mpg"), PathPattern("{video}.raw.mpg"), shared=frozenset({"video"})
    )


def test_overlaps_shared_only_strips_aligned_prefixes():
    # An unshared or misaligned binding falls through to the conservative
    # check unchanged — sharing never invents an overlap and never removes
    # one that rigidity cannot rule out.
    a = PathPattern("out/{video}.mpg")
    b = PathPattern("out/{video}/{scene}.mpg")
    assert overlaps(a, b, shared=frozenset({"scene"}))  # video not shared
    # Different literal prefixes: nothing strips, ordinary answer stands.
    assert not overlaps(
        PathPattern("x/{v}.mpg"), PathPattern("y/{v}.mpg"), shared=frozenset({"v"})
    )
    # A shared binding at different positions is not stripped: the answer is
    # whatever the conservative check says (here its documented probe
    # under-approximation — v="a" would collide — unchanged by sharing).
    assert overlaps(PathPattern("a/{v}.txt"), PathPattern("{v}/a.txt")) == overlaps(
        PathPattern("a/{v}.txt"), PathPattern("{v}/a.txt"), shared=frozenset({"v"})
    )
    # One side fully consumed: equal templates overlap, an extension does not.
    assert overlaps(PathPattern("{v}.txt"), PathPattern("{v}.txt"), shared=frozenset({"v"}))
    assert not overlaps(
        PathPattern("{v}.txt"), PathPattern("{v}.txt.bak"), shared=frozenset({"v"})
    )
