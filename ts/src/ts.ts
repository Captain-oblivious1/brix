/**
 * Typed TypeScript actions — the TS mirror of `src/brix/ts.py`. A language skin:
 * it lowers to the generic core (Stage 1), adding no new engine behaviour.
 *
 * Covers `CompileAction` (tsc → out_dir, with the cross-module **ts-dts** lens
 * and automatic import tracking), `RunTestAction` (per-file vitest impact
 * selection), `TypedocAction`, and the `dts`/`DtsFile`/`DtsGlob` content lens.
 *
 * Deferred (faithful follow-ons): `ts.npm` + the shared `DepSet` (a filesystem
 * resolver, shared with java/cpp), and the atomic staging-hazard guard for
 * self-resolved imports (build-time validation, not part of the wire spec).
 */

import * as fs from "node:fs";
import * as nodePath from "node:path";

import { Action, ExternalProcessAction } from "./actions";
import {
  DataExpr,
  DataUnion,
  FileData,
  GlobData,
  ToolData,
  ValueData,
} from "./data";
import { DepSet } from "./deps";

function optionsExpr(options: string[] | DataExpr | undefined): DataExpr {
  if (options instanceof DataExpr) return options;
  return new ValueData((options ?? []).join(" "));
}

/**
 * A `.d.ts` declaration compared through the ts-dts lens, not its bytes: a
 * consumer recompiles on a public-API change, not an implementation-only change.
 * brix.ts owns this lens; the Rust core computes the `ts-dts` digest.
 */
export class DtsFile extends FileData {
  constructor(path: string) {
    super(path);
    this.contentKey = "ts-dts";
  }
}

export class DtsGlob extends GlobData {
  constructor(template: string, exclude: readonly string[] = []) {
    super(template, exclude);
    this.contentKey = "ts-dts";
  }
}

/**
 * View an expression through the ts-dts lens: every plain file/glob atom becomes
 * its DtsFile/DtsGlob counterpart; non-file atoms and already-lensed atoms pass
 * through unchanged.
 */
export function dts(expr: DataExpr): DataExpr {
  const lensed = expr.atoms().map((atom) => {
    if (atom.constructor === FileData) return new DtsFile((atom as FileData).path);
    if (atom.constructor === GlobData) {
      const g = atom as GlobData;
      return new DtsGlob(g.template, [...g.exclude]);
    }
    return atom;
  });
  return lensed.length === 1 ? lensed[0] : new DataUnion(...lensed);
}

/** Recursively collect `.d.ts` under `dir`, as paths relative to `root` (posix). */
function walkDts(root: string, dir: string, out: string[]): void {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = nodePath.join(dir, entry.name);
    if (entry.isDirectory()) {
      walkDts(root, full, out);
    } else if (entry.isFile() && entry.name.endsWith(".d.ts")) {
      out.push(nodePath.relative(root, full).split(nodePath.sep).join("/"));
    }
  }
}

/** The workspace-relative `.d.ts` a package contributes: its own type tree plus
 * its DefinitelyTyped `@types/<name>` tree, if present. Nested `node_modules`
 * are skipped (npm hoists transitive deps; descending would over-track). Scoped
 * names map to DefinitelyTyped's mangled dir (`@scope/pkg` -> `scope__pkg`).
 * Mirrors `_resolve_npm_types`. */
function resolveNpmTypes(base: string, nodeModules: string, name: string): string[] {
  const atTypes = name.startsWith("@") ? name.slice(1).replace(/\//g, "__") : name;
  const candidates = [
    nodePath.join(nodeModules, name),
    nodePath.join(nodeModules, "@types", atTypes),
  ];
  const out: string[] = [];
  for (const relDir of candidates) {
    const absDir = nodePath.join(base, relDir);
    let isDir = false;
    try {
      isDir = fs.statSync(absDir).isDirectory();
    } catch {
      isDir = false;
    }
    if (!isDir) continue;
    const rels: string[] = [];
    walkDts(absDir, absDir, rels);
    // A nested install; the top-level one is tracked already.
    const kept = rels.filter((rel) => !rel.split("/").includes("node_modules"));
    kept.sort();
    for (const rel of kept) {
      const abs = nodePath.join(absDir, rel);
      out.push(nodePath.relative(base, abs).split(nodePath.sep).join("/"));
    }
  }
  return out;
}

export interface NpmOptions {
  nodeModules?: string;
  root?: string;
}

/**
 * Resolve npm packages' TypeScript type declarations into a `DepSet` — the TS
 * mirror of `ts.npm`. brix does not run npm; you `npm install` and this adapter
 * consumes the result. For each package it collects that package's `.d.ts` under
 * `node_modules` (and `@types/<name>` for DefinitelyTyped), so upgrading a
 * dependency whose types changed re-stales the compile. Pass the result as
 * `ts.CompileAction({ deps })` — the consumer applies the ts-dts lens.
 *
 * Reads the real filesystem now (per build, like the Maven adapters). Paths are
 * relative to `root`. Throws if a named package has no discoverable `.d.ts`.
 */
export function npm(names: string[], opts: NpmOptions = {}): DepSet {
  const nodeModules = opts.nodeModules ?? "node_modules";
  const base = opts.root ?? ".";
  const artifacts: string[] = [];
  for (const name of names) {
    const found = resolveNpmTypes(base, nodeModules, name);
    if (found.length === 0) {
      throw new Error(
        `ts.npm: no .d.ts type declarations for '${name}' under ${nodeModules}/ — ` +
          `is it installed, and does it ship types (or need @types/${name})?`,
      );
    }
    artifacts.push(...found);
  }
  // Order-preserving de-dup (first occurrence wins), like Python's dict.fromkeys.
  return new DepSet([...new Set(artifacts)]);
}

export interface RunTestOptions {
  testFile: string | FileData;
  sources?: (string | DataExpr)[];
  runner?: ToolData;
  name?: string;
  args?: string[] | DataExpr;
}

export class RunTestAction extends Action {
  private readonly testFile: FileData;
  private readonly sources: (string | DataExpr)[];
  private readonly runner: ToolData;
  private readonly name: string;
  private readonly args?: string[] | DataExpr;

  constructor(opts: RunTestOptions) {
    super();
    this.testFile =
      opts.testFile instanceof FileData ? opts.testFile : new FileData(opts.testFile);
    this.sources = opts.sources ?? [];
    this.runner = opts.runner ?? new ToolData("vitest");
    this.name = opts.name ?? `vitest ${this.testFile.path}`;
    this.args = opts.args;
  }

  lower(): Action[] {
    const inputs: Record<string, DataExpr> = {
      vitest: this.runner,
      test: this.testFile, // both the command arg and a byte-lens input
    };
    this.sources.forEach((src, i) => {
      inputs[`src${i}`] = src instanceof DataExpr ? src : new FileData(src);
    });
    inputs.args = optionsExpr(this.args);
    return [
      new ExternalProcessAction({
        name: this.name,
        commandLine: "{vitest} run {test} {args}",
        inputs,
        outputs: {}, // exit code only — replays on the input fingerprint
      }),
    ];
  }
}

export interface CompileOptions {
  sources: DataExpr;
  outDir: string;
  tsc?: ToolData;
  options?: string[] | DataExpr;
  declaration?: boolean;
  dependencies?: DataExpr[];
  /** A resolved npm DepSet (`ts.npm`): its `.d.ts` artifacts join `dependencies`
   * and are tracked exactly like an upstream module's declaration — ts-dts-lensed
   * and resolved on disk by tsc, so a dependency's type change rebuilds, an
   * impl-only change replays. */
  deps?: DepSet;
  trackImports?: boolean;
  python?: ToolData;
}

export class CompileAction extends Action {
  private readonly sources: DataExpr;
  private readonly outDir: string;
  private readonly tsc: ToolData;
  private readonly options?: string[] | DataExpr;
  private readonly declaration: boolean;
  private readonly dependencies: DataExpr[];
  private readonly trackImports: boolean;
  private readonly python: ToolData;

  constructor(opts: CompileOptions) {
    super();
    this.sources = opts.sources;
    this.outDir = opts.outDir;
    this.tsc = opts.tsc ?? new ToolData("tsc");
    this.options = opts.options;
    this.declaration = opts.declaration ?? true;
    // A resolved npm DepSet's .d.ts join the declared upstream dependencies and
    // are tracked identically (ts-dts-lensed); mirrors brix.ts's fold order
    // (explicit dependencies first, then the npm-resolved artifacts).
    this.dependencies = [
      ...(opts.dependencies ?? []),
      ...(opts.deps ? opts.deps.files() : []),
    ];
    this.trackImports = opts.trackImports ?? true;
    this.python = opts.python ?? new ToolData("python3");
  }

  lower(): Action[] {
    const outputs: Record<string, DataExpr> = {
      out_dir: new FileData(this.outDir),
      js: new GlobData(`${this.outDir}/{name}.js`),
    };
    let declFlag = "";
    if (this.declaration) {
      declFlag = "--declaration ";
      outputs.dts = new GlobData(`${this.outDir}/{name}.d.ts`);
    }

    const inputs: Record<string, DataExpr> = {
      tsc: this.tsc,
      options: optionsExpr(this.options),
      sources: this.sources,
    };
    if (this.dependencies.length) {
      // ts-dts-lensed; tsc finds them on disk, but declaring them tracks the
      // upstream API surface (impl-only upstream change → this module replays).
      inputs.types = dts(new DataUnion(...this.dependencies));
    }

    if (this.trackImports) {
      // Import tracking wraps a brix-shipped `--listFiles` recorder. Referencing
      // that helper portably (identically to the Python skin, without baking a
      // machine path into the fingerprint) needs the asset mechanism, whose
      // engine half is not built yet (docs/boundary-schema.md, roadmap Tier C).
      // Rather than emit a command that diverges from the Python skin and only
      // works when brix is importable by the tsc subprocess, refuse it clearly.
      throw new Error(
        "ts.CompileAction: import tracking (trackImports) is not available in the " +
          "TypeScript frontend yet — it needs brix's asset mechanism (pending). " +
          "Pass trackImports:false for a bare tsc compile — but note that " +
          "undeclared cross-file imports are then not tracked, so editing such a " +
          "file will not re-stale this compile until the asset mechanism lands.",
      );
    }

    const tscCommand = `{tsc} {options} ${declFlag}--outDir {out_dir} {sources}`;
    return [
      new ExternalProcessAction({
        name: `tsc -> ${this.outDir}`,
        commandLine: tscCommand,
        inputs,
        outputs,
      }),
    ];
  }
}

export interface TypedocOptions {
  entryPoints: DataExpr;
  outputDir: string;
  typedoc?: ToolData;
  options?: string[] | DataExpr;
}

export class TypedocAction extends Action {
  private readonly entryPoints: DataExpr;
  private readonly outputDir: string;
  private readonly typedoc: ToolData;
  private readonly options?: string[] | DataExpr;

  constructor(opts: TypedocOptions) {
    super();
    this.entryPoints = opts.entryPoints;
    this.outputDir = opts.outputDir;
    this.typedoc = opts.typedoc ?? new ToolData("typedoc");
    this.options = opts.options;
  }

  lower(): Action[] {
    return [
      new ExternalProcessAction({
        name: `typedoc -> ${this.outputDir}`,
        commandLine: "{typedoc} --out {output_dir} {options} {entry_points}",
        inputs: {
          typedoc: this.typedoc,
          options: optionsExpr(this.options),
          entry_points: this.entryPoints,
        },
        outputs: {
          output_dir: new FileData(this.outputDir),
          files: new GlobData(`${this.outputDir}/{name}`),
        },
      }),
    ];
  }
}
