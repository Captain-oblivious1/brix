"""Installed ``brix-workerd`` launcher for the Rust worker-holder daemon."""


def main() -> None:
    from . import brix_py

    brix_py.workerd_main()
