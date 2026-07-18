#!/usr/bin/env python3
"""A trivial shared build step, used by every package.

Usage: step.py <label> <input> <output>
Writes ``<label>(<input contents>)`` so lineage is visible in the output bytes:
core produces ``core(hello)``, api produces ``api(core(hello))``, and so on. A
change anywhere upstream shows up, byte-for-byte, downstream.

It lives at the workspace root (``tools/``) and is an input of every package, so
editing it is the "shared input outside any package's subtree" case that makes
pattern-based selection load *everything* — the soundness demonstration in
verify_lazy.py.
"""

import sys
from pathlib import Path

label, src, dest = sys.argv[1], sys.argv[2], sys.argv[3]
out = Path(dest)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(f"{label}({Path(src).read_text().strip()})")
