"""The ``jvm-abi`` content identity: hash a jar/class's ABI, not its bytes.

A dependent module recompiles when a dependency's *API* changes, not when its
*implementation* does. So the fingerprint of a dependent should contain a
dependency jar's ABI hash rather than its byte hash. This module computes that
hash from Java class files directly — a small, pure-Python class-file parser,
no JVM required (the format is stable and simple).

The hash **over-approximates**: anything that can affect the compilation of a
dependent is included, so a false "changed" (needless rebuild) is possible but
a false "unchanged" (stale build) is not. Included, per class (JVMS ch. 4):

- class name, access flags, superclass, interfaces, generic signature;
- every **non-private** field and method: name, descriptor, access flags,
  generic signature, ``throws`` (Exceptions), declaration + parameter
  annotations, and an annotation method's default value;
- ``static final`` **constant values** (javac inlines compile-time constants
  into dependents — omitting them is the classic unsound "ABI hash");
- **all** annotations (both runtime-visible and runtime-invisible/CLASS
  retention) on the class and its non-private members;
- nest/inner-class and sealed (PermittedSubclasses) attributes that affect
  accessibility and subclassing.

Excluded: method bodies (Code), private members, debug info, line numbers,
the source file name, and the bytecode version (a recompile with a newer JDK
is not an API change; a JDK change already invalidates via the tool/worker
fingerprint elsewhere).

Determinism (this digest is a cross-machine cache key): every constant-pool
reference is resolved to a value, so the digest never depends on constant-pool
order; members are sorted by (name, descriptor); UTF-8-derived names are
represented as raw-bytes hex (no text-decoding step to disagree on across
languages); float/double constants as their raw IEEE-754 bits (no
float-formatting hazard). A jar's classes are digested in sorted-name order
and their digests concatenated.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zipfile
from pathlib import Path

# Constant-pool tags (JVMS Table 4.4-B).
_UTF8 = 1
_INTEGER = 3
_FLOAT = 4
_LONG = 5
_DOUBLE = 6
_CLASS = 7
_STRING = 8
_FIELDREF = 9
_METHODREF = 10
_INTERFACE_METHODREF = 11
_NAME_AND_TYPE = 12
_METHOD_HANDLE = 15
_METHOD_TYPE = 16
_DYNAMIC = 17
_INVOKE_DYNAMIC = 18
_MODULE = 19
_PACKAGE = 20

ACC_PRIVATE = 0x0002


class ClassFormatError(Exception):
    """The bytes are not a class file we can parse."""


class _Reader:
    __slots__ = ("d", "p")

    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def u1(self) -> int:
        (v,) = struct.unpack_from(">B", self.d, self.p)
        self.p += 1
        return v

    def u2(self) -> int:
        (v,) = struct.unpack_from(">H", self.d, self.p)
        self.p += 2
        return v

    def u4(self) -> int:
        (v,) = struct.unpack_from(">I", self.d, self.p)
        self.p += 4
        return v

    def s4(self) -> int:
        (v,) = struct.unpack_from(">i", self.d, self.p)
        self.p += 4
        return v

    def s8(self) -> int:
        (v,) = struct.unpack_from(">q", self.d, self.p)
        self.p += 8
        return v

    def raw4(self) -> int:
        """Four bytes as an unsigned int, keeping the exact bit pattern."""
        return self.u4()

    def raw8(self) -> int:
        (v,) = struct.unpack_from(">Q", self.d, self.p)
        self.p += 8
        return v

    def take(self, n: int) -> bytes:
        b = self.d[self.p : self.p + n]
        self.p += n
        return b


def _parse_constant_pool(r: _Reader) -> list:
    count = r.u2()
    pool: list = [None] * count
    i = 1
    while i < count:
        tag = r.u1()
        if tag == _UTF8:
            length = r.u2()
            pool[i] = (_UTF8, r.take(length))  # raw modified-UTF-8 bytes
        elif tag == _INTEGER:
            pool[i] = (_INTEGER, r.s4())
        elif tag == _FLOAT:
            pool[i] = (_FLOAT, r.raw4())  # IEEE-754 bits, not the float
        elif tag == _LONG:
            pool[i] = (_LONG, r.s8())
        elif tag == _DOUBLE:
            pool[i] = (_DOUBLE, r.raw8())  # IEEE-754 bits
        elif tag in (_CLASS, _STRING, _METHOD_TYPE, _MODULE, _PACKAGE):
            pool[i] = (tag, r.u2())
        elif tag in (_FIELDREF, _METHODREF, _INTERFACE_METHODREF, _NAME_AND_TYPE):
            pool[i] = (tag, r.u2(), r.u2())
        elif tag == _METHOD_HANDLE:
            pool[i] = (tag, r.u1(), r.u2())
        elif tag in (_DYNAMIC, _INVOKE_DYNAMIC):
            pool[i] = (tag, r.u2(), r.u2())
        else:
            raise ClassFormatError(f"unknown constant-pool tag {tag}")
        # Long and Double occupy two pool slots (JVMS 4.4.5).
        i += 2 if tag in (_LONG, _DOUBLE) else 1
    return pool


def _utf8(pool: list, index: int) -> str:
    """A Utf8 constant as hex of its raw bytes — encoding-agnostic and
    deterministic across languages."""
    entry = pool[index]
    if entry is None or entry[0] != _UTF8:
        raise ClassFormatError(f"constant {index} is not Utf8")
    return entry[1].hex()


def _utf8_bytes(pool: list, index: int) -> bytes:
    entry = pool[index]
    if entry is None or entry[0] != _UTF8:
        raise ClassFormatError(f"constant {index} is not Utf8")
    return entry[1]


def _class_name(pool: list, index: int):
    if index == 0:
        return None
    tag, name_index = pool[index][0], pool[index][1]
    if tag != _CLASS:
        raise ClassFormatError(f"constant {index} is not Class")
    return _utf8(pool, name_index)


def _const_value(pool: list, index: int):
    """A canonical, order-independent form of a loadable constant."""
    entry = pool[index]
    tag = entry[0]
    if tag in (_INTEGER, _LONG):
        return [tag, entry[1]]
    if tag in (_FLOAT, _DOUBLE):
        return [tag, entry[1]]  # raw IEEE-754 bits
    if tag == _STRING:
        return [_STRING, _utf8(pool, entry[1])]
    if tag == _CLASS:
        return [_CLASS, _utf8(pool, entry[1])]
    # MethodHandle/MethodType/Dynamic constants: fall back to structural refs.
    return [tag, *entry[1:]]


def _parse_attributes(r: _Reader) -> dict:
    """Read an attribute table into {name_bytes: [info_bytes, ...]}.

    Names are kept as raw bytes so matching (b"Signature") needs no decode;
    attribute names are always ASCII in practice.
    """
    out: dict = {}
    count = r.u2()
    for _ in range(count):
        name_index = r.u2()
        length = r.u4()
        info = r.take(length)
        out.setdefault(name_index, []).append(info)
    return out


def _named_attributes(pool: list, raw: dict) -> dict:
    named: dict = {}
    for name_index, infos in raw.items():
        named.setdefault(_utf8_bytes(pool, name_index), []).extend(infos)
    return named


# -- annotation canonicalization (JVMS 4.7.16) -------------------------------


def _read_element_value(pool: list, r: _Reader):
    tag = r.u1()
    t = chr(tag)
    if t == "s":
        # A String-valued element: the index points *directly* at a Utf8
        # constant (not a String constant), so carry it like the `c`/`e`
        # tags do, not through `_const_value` (which handles loadable
        # constants and would leak the raw Utf8 bytes).
        return [t, _utf8(pool, r.u2())]
    if t in "BCDFIJSZ":  # primitive-valued elements: Integer/Long/Float/Double
        return [t, _const_value(pool, r.u2())]
    if t == "e":  # enum
        return [t, _utf8(pool, r.u2()), _utf8(pool, r.u2())]
    if t == "c":  # class
        return [t, _utf8(pool, r.u2())]
    if t == "@":  # nested annotation
        return [t, _read_annotation(pool, r)]
    if t == "[":  # array
        n = r.u2()
        return [t, [_read_element_value(pool, r) for _ in range(n)]]
    raise ClassFormatError(f"bad annotation element tag {tag!r}")


def _read_annotation(pool: list, r: _Reader):
    type_index = r.u2()
    n = r.u2()
    pairs = []
    for _ in range(n):
        name = _utf8(pool, r.u2())
        pairs.append([name, _read_element_value(pool, r)])
    pairs.sort()  # element order is not significant
    return [_utf8(pool, type_index), pairs]


def _read_annotations(pool: list, info: bytes):
    r = _Reader(info)
    n = r.u2()
    anns = [_read_annotation(pool, r) for _ in range(n)]
    anns.sort()
    return anns


def _read_parameter_annotations(pool: list, info: bytes):
    r = _Reader(info)
    num_params = r.u1()
    params = []
    for _ in range(num_params):
        n = r.u2()
        anns = [_read_annotation(pool, r) for _ in range(n)]
        anns.sort()
        params.append(anns)
    return params  # per-parameter order IS significant; do not sort


def _declaration_annotations(pool: list, named: dict):
    anns = []
    for attr in (b"RuntimeVisibleAnnotations", b"RuntimeInvisibleAnnotations"):
        for info in named.get(attr, []):
            anns.extend(_read_annotations(pool, info))
    anns.sort()
    return anns


# -- members and class -------------------------------------------------------


def _member_abi(pool: list, r: _Reader):
    """One field or method; returns None for a private member (excluded)."""
    access = r.u2()
    name = _utf8(pool, r.u2())
    descriptor = _utf8(pool, r.u2())
    named = _named_attributes(pool, _parse_attributes(r))
    if access & ACC_PRIVATE:
        return None
    abi: dict = {"access": access, "name": name, "descriptor": descriptor}
    if b"Signature" in named:
        abi["signature"] = _utf8_index(pool, named[b"Signature"][0])
    if b"ConstantValue" in named:  # static final inlined constants
        (info,) = named[b"ConstantValue"][:1]
        abi["constant"] = _const_value(pool, _Reader(info).u2())
    if b"Exceptions" in named:  # throws clause
        r2 = _Reader(named[b"Exceptions"][0])
        n = r2.u2()
        abi["throws"] = sorted(_class_name(pool, r2.u2()) for _ in range(n))
    decl = _declaration_annotations(pool, named)
    if decl:
        abi["annotations"] = decl
    params = []
    for attr in (b"RuntimeVisibleParameterAnnotations", b"RuntimeInvisibleParameterAnnotations"):
        for info in named.get(attr, []):
            params.append(_read_parameter_annotations(pool, info))
    if params:
        abi["param_annotations"] = params
    if b"AnnotationDefault" in named:  # annotation-type element defaults
        abi["default"] = _read_element_value(pool, _Reader(named[b"AnnotationDefault"][0]))
    return abi


def _utf8_index(pool: list, info: bytes) -> str:
    """A Signature attribute: a single u2 index into a Utf8 constant."""
    return _utf8(pool, _Reader(info).u2())


def class_abi(data: bytes) -> dict:
    """The canonical ABI structure of one class file."""
    r = _Reader(data)
    if r.u4() != 0xCAFEBABE:
        raise ClassFormatError("not a class file (bad magic)")
    r.u2()  # minor version (excluded from ABI)
    r.u2()  # major version (excluded)
    pool = _parse_constant_pool(r)
    access = r.u2()
    this_class = _class_name(pool, r.u2())
    super_class = _class_name(pool, r.u2())
    interfaces = sorted(_class_name(pool, r.u2()) for _ in range(r.u2()))

    fields = []
    for _ in range(r.u2()):
        m = _member_abi(pool, r)
        if m is not None:
            fields.append(m)
    methods = []
    for _ in range(r.u2()):
        m = _member_abi(pool, r)
        if m is not None:
            methods.append(m)
    named = _named_attributes(pool, _parse_attributes(r))

    fields.sort(key=lambda m: (m["name"], m["descriptor"]))
    methods.sort(key=lambda m: (m["name"], m["descriptor"]))
    abi: dict = {
        "access": access,
        "name": this_class,
        "super": super_class,
        "interfaces": interfaces,
        "fields": fields,
        "methods": methods,
    }
    if b"Signature" in named:
        abi["signature"] = _utf8_index(pool, named[b"Signature"][0])
    decl = _declaration_annotations(pool, named)
    if decl:
        abi["annotations"] = decl
    # Accessibility / subclassing structure.
    if b"NestHost" in named:
        abi["nest_host"] = _class_name(pool, _Reader(named[b"NestHost"][0]).u2())
    for attr, key in ((b"NestMembers", "nest_members"), (b"PermittedSubclasses", "permitted")):
        if attr in named:
            r2 = _Reader(named[attr][0])
            n = r2.u2()
            abi[key] = sorted(_class_name(pool, r2.u2()) for _ in range(n))
    if b"InnerClasses" in named:
        abi["inner"] = _inner_classes(pool, named[b"InnerClasses"][0])
    return abi


def _inner_classes(pool: list, info: bytes):
    r = _Reader(info)
    n = r.u2()
    out = []
    for _ in range(n):
        inner = _class_name(pool, r.u2())
        outer_idx = r.u2()
        outer = _class_name(pool, outer_idx) if outer_idx else None
        name_idx = r.u2()
        simple = _utf8(pool, name_idx) if name_idx else None
        flags = r.u2()
        out.append([inner, outer, simple, flags])
    out.sort()
    return out


def _digest_bytes(abi: dict) -> str:
    payload = json.dumps(abi, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def digest_class(data: bytes) -> str:
    return _digest_bytes(class_abi(data))


def digest_jar(path: Path) -> str:
    """ABI digest of a jar: per-class digests in sorted-name order, concatenated
    and hashed. Non-class entries (resources, manifest) are ignored."""
    parts = []
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".class"):
                parts.append(digest_class(zf.read(name)))
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def digest(path: Path) -> str:
    """ABI digest of a jar or a single ``.class`` file."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        return digest_jar(path)
    return digest_class(path.read_bytes())
