from brix.data import (
    DataFactory,
    DataUnion,
    FileData,
    GlobData,
    RegexData,
    ToolData,
    ValueData,
)

data = DataFactory()


def test_factory_kinds():
    assert isinstance(data.file("src/a.cpp"), FileData)
    assert isinstance(data.glob("src/{n}.cpp"), GlobData)
    assert isinstance(data.regex(r"src/(?P<n>\w+)\.cpp"), RegexData)
    assert isinstance(data.tool("c++"), ToolData)
    assert isinstance(data.value("-O2"), ValueData)


def test_union_flattens():
    u = data.glob("a/{n}.c") + data.glob("b/{n}.c") + data.file("c.c")
    assert isinstance(u, DataUnion)
    assert len(u.parts) == 3
    assert len(list(u.atoms())) == 3


def test_atoms_of_atom():
    f = data.file("x.txt")
    assert list(f.atoms()) == [f]


def test_file_substitute():
    f = data.file("src/{name}.cpp").substitute({"name": "foo"})
    assert f.path == "src/foo.cpp"


def test_regex_match():
    r = data.regex(r"src/(?P<name>[0-9]+)\.cpp")
    assert r.match("src/123.cpp") == {"name": "123"}
    assert r.match("src/abc.cpp") is None


def test_glob_minus_str_and_list():
    g = data.glob("src/{n}.cc")
    assert (g - "*_test.cc").exclude == ("*_test.cc",)
    assert (g - ["*_test.cc", "u/win.cc"]).exclude == ("*_test.cc", "u/win.cc")


def test_glob_minus_is_immutable_and_chains():
    g = data.glob("src/{n}.cc")
    once = g - "*_test.cc"
    twice = once - "*_bench.cc"
    assert g.exclude == ()  # the original is untouched
    assert once.exclude == ("*_test.cc",)
    assert twice.exclude == ("*_test.cc", "*_bench.cc")
    assert twice.pattern.template == "src/{n}.cc"


def test_glob_minus_preserves_lens_and_survives_substitute():
    from brix.java import AbiGlob

    g = AbiGlob("out/{v}/{s}.mpg") - "*.raw.mpg"
    sub = g.substitute({"v": "intro"})
    assert sub.pattern.template == "out/intro/{s}.mpg"
    # type(self) reconstruction keeps the lens across both `-` and substitute.
    assert isinstance(sub, AbiGlob)
    assert sub.content_key == "jvm-abi"
    # Excludes carry through untouched — substitution never rewrites them.
    assert sub.exclude == ("*.raw.mpg",)


def test_glob_minus_wrong_type_raises():
    import pytest

    with pytest.raises(TypeError):
        data.glob("src/{n}.cc") - 3
    with pytest.raises(TypeError):
        data.glob("src/{n}.cc") - ["ok", 3]


def test_glob_minus_repr():
    assert repr(data.glob("s/{n}.cc") - "*_t.cc") == "data.glob('s/{n}.cc') - ['*_t.cc']"
