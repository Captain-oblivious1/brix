"""CI guard for the end-to-end monorepo dogfood.

`examples/monorepo_dogfood.py` is the one place the whole stack runs together as
the trial will: git-driven `load_affected` (changed=None) → git-accelerated
incremental build on the default (Rust) engine, over a hierarchical C monorepo
with a derived cross-package dependency. It self-checks and returns non-zero on
any surprise; this just runs it so a regression in that composition is caught.

Self-contained (its own temp workspace); needs cc/ar/git.
"""

import shutil

import pytest

pytestmark = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("cc", "ar", "git")),
    reason="needs cc, ar, and git",
)


def test_monorepo_dogfood_passes():
    from examples import monorepo_dogfood

    assert monorepo_dogfood.main() == 0
