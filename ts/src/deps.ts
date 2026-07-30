/**
 * Resolved third-party dependency sets — the TS mirror of `src/brix/deps.py`.
 *
 * A `DepSet` is what a resolver adapter (`java.pom`, `cpp.pkg_config`, …) hands
 * back: the artifact files a build compiles or links against, plus any flags
 * they require. brix does not resolve dependencies itself — it consumes a
 * resolver's output as tracked file inputs (docs/dependencies.md). The *consumer*
 * applies the content lens (a compile classpath is ABI-lensed, a runtime one
 * byte-lensed), so a `DepSet` stays lens-neutral.
 *
 * It grows with `.plus` (ordered merge, first occurrence wins) and shrinks with
 * `.excluding` (exclude by fnmatch) — the method forms of Python's `+`/`-`, since
 * TS has no operator overloading (the same sanctioned deviation as DataExpr's
 * `.plus`/`.excluding`). Order is preserved because for native linking it is
 * load-bearing (`-lfoo -lbar` ≠ the reverse); for a JVM classpath it is harmless.
 */

import { FileData } from "./data";

/** Order-preserving de-dup (first occurrence wins) — overlapping transitive
 * closures otherwise repeat artifacts, and first-wins keeps link order stable. */
function dedup(paths: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const p of paths) {
    if (!seen.has(p)) {
      seen.add(p);
      out.push(p);
    }
  }
  return out;
}

/** Translate a Python-`fnmatch` pattern (`*`, `?`, `[seq]`, `[!seq]`) into an
 * anchored RegExp, mirroring `fnmatch.translate` closely enough for artifact
 * paths (which contain no newlines). Used by `.excluding`. */
function fnmatchToRegExp(pat: string): RegExp {
  let re = "";
  let i = 0;
  const n = pat.length;
  while (i < n) {
    const c = pat[i++];
    if (c === "*") {
      re += ".*";
    } else if (c === "?") {
      re += ".";
    } else if (c === "[") {
      let j = i;
      if (j < n && (pat[j] === "!" || pat[j] === "]")) j++;
      while (j < n && pat[j] !== "]") j++;
      if (j >= n) {
        re += "\\[";
      } else {
        let stuff = pat.slice(i, j).replace(/\\/g, "\\\\");
        if (stuff.startsWith("!")) stuff = "^" + stuff.slice(1);
        re += "[" + stuff + "]";
        i = j + 1;
      }
    } else {
      re += c.replace(/[.^$+{}()|\\/[\]*?]/g, (m) => "\\" + m);
    }
  }
  return new RegExp("^(?:" + re + ")$", "s");
}

function basename(path: string): string {
  const idx = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return idx === -1 ? path : path.slice(idx + 1);
}

export class DepSet {
  readonly artifacts: readonly string[];
  readonly compileFlags: readonly string[];
  readonly linkFlags: readonly string[];

  constructor(
    artifacts: readonly string[] = [],
    compileFlags: readonly string[] = [],
    linkFlags: readonly string[] = [],
  ) {
    this.artifacts = artifacts;
    this.compileFlags = compileFlags;
    this.linkFlags = linkFlags;
  }

  /** Ordered merge with another DepSet, or append an artifact (a FileData/path). */
  plus(other: DepSet | FileData | string): DepSet {
    if (other instanceof DepSet) {
      return new DepSet(
        dedup([...this.artifacts, ...other.artifacts]),
        [...this.compileFlags, ...other.compileFlags],
        [...this.linkFlags, ...other.linkFlags],
      );
    }
    const path = other instanceof FileData ? other.path : other;
    return new DepSet(dedup([...this.artifacts, path]), this.compileFlags, this.linkFlags);
  }

  /** Drop artifacts matching any fnmatch pattern (against the full path or its
   * basename) — e.g. excluding a transitively-pulled `commons-logging-*.jar`. */
  excluding(patterns: string | readonly string[]): DepSet {
    const pats = (typeof patterns === "string" ? [patterns] : patterns).map((p) => ({
      full: fnmatchToRegExp(p),
    }));
    const kept = this.artifacts.filter((a) => {
      const name = basename(a);
      return !pats.some((p) => p.full.test(a) || p.full.test(name));
    });
    return new DepSet(kept, this.compileFlags, this.linkFlags);
  }

  /** The artifacts as byte-lens file atoms (the consumer re-lenses). */
  files(): FileData[] {
    return this.artifacts.map((a) => new FileData(a));
  }

  isEmpty(): boolean {
    return (
      this.artifacts.length === 0 &&
      this.compileFlags.length === 0 &&
      this.linkFlags.length === 0
    );
  }
}
