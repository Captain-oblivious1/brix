# Packaging and releases

Brix uses Maturin as its PEP 517 build backend. A platform wheel contains:

- the `brix` Python package;
- the `brix.brix_py` native extension and shared Rust engine;
- the `brix-workerd` console launcher, whose daemon implementation is compiled
  into that extension; and
- the Java worker and Spring Boot repackager sources under `brix/_java/`.

End users installing a published wheel need Python 3.11 or newer. They do not
need Rust, Cargo, a C compiler, or the Brix source checkout:

```sh
python -m pip install brix
```

## Local development

An editable install builds the extension through Maturin and installs the
worker launcher:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Direct Cargo builds remain useful when working only on the engine:

```sh
cd rust
cargo test --workspace
cargo build --release -p brix-py -p brix-workerd
```

The Python runtime still recognizes standalone development artifacts under
`rust/target/{release,debug}`. Installed wheels import `brix.brix_py` directly.

## Build and verify a wheel

Run the release smoke test from the repository root:

```sh
python scripts/wheel_smoke.py
```

It builds a release wheel, installs it without dependencies into a new virtual
environment, changes to a directory outside the checkout, runs a real build on
the Rust engine, verifies the Java resources, and invokes the installed worker
launcher. Temporary wheels and environments are removed afterward.

Published Linux wheels should be built in an appropriate manylinux environment;
a normal host build may receive a host-specific `manylinux` or `linux` tag based
on its linked glibc version. macOS requires its own native wheel build. The
persistent worker daemon currently uses Unix sockets, so Windows wheel support
remains future work rather than a release claim.
