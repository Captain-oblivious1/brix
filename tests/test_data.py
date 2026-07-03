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
