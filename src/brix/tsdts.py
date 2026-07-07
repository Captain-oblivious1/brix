"""TypeScript `.d.ts` content identity: fingerprint a declaration file by its
*public surface*, ignoring comments and formatting, so a consumer recompiles on
an interface change but not on an implementation-only change or a doc edit.

Most of the pruning is structural and comes for free: `tsc`'s declaration emit
writes only the public API, so an implementation-only change produces a
byte-identical `.d.ts` and never invalidates consumers. This plugin adds
robustness on top — stripping comments and collapsing formatting whitespace so
JSDoc and reformatting churn don't invalidate either.

Normalization is deliberately conservative: it strips comments and collapses
whitespace *outside* string/template literals (whitespace *inside* a string
literal type such as ``"a  b"`` is significant and preserved), and it never
sorts or reorders declarations. It can therefore only over-approximate a change
(a reorder still invalidates), never miss one — the safe direction for a cache
key.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def normalize(text: str) -> str:
    """Strip `//` and `/* */` comments (respecting string and template
    literals) and collapse each run of formatting whitespace to a single space.
    Whitespace inside a string/template literal is preserved verbatim."""
    out: list[str] = []
    prev_space = True  # true so leading whitespace is dropped
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and text[i + 1 : i + 2] == "/"):
                i += 1
            i += 2  # skip the closing */
        elif c in "'\"`":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                d = text[i]
                out.append(d)
                if d == "\\" and i + 1 < n:  # escape: take the next char verbatim
                    out.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                if d == quote:
                    break
            prev_space = False
        elif c.isspace():
            if not prev_space:
                out.append(" ")
                prev_space = True
            i += 1
        else:
            out.append(c)
            prev_space = False
            i += 1
    return "".join(out).strip()


def digest(path: Path) -> str:
    """Identity digest of a `.d.ts` file. Raises (so the engine falls back to a
    ``:raw`` byte hash) on a file that is not decodable UTF-8 text."""
    text = Path(path).read_text(encoding="utf-8")  # UnicodeDecodeError -> :raw
    return hashlib.sha256(normalize(text).encode()).hexdigest()
