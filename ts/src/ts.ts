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

import { Action, ExternalProcessAction } from "./actions";
import {
  DataExpr,
  DataUnion,
  FileData,
  GlobData,
  ToolData,
  ValueData,
} from "./data";

// brix's tsc wrapper: runs the compile with --listFiles and records the files
// tsc actually read as a Makefile depfile (the TS counterpart of `cc -MMD`).
// Invoked as a module (like the pytest plugin) so it is location-independent.
const WRAPPER_MODULE = "brix._ts.tsc_listfiles";

function optionsExpr(options: string[] | DataExpr | undefined): DataExpr {
  if (options instanceof DataExpr) return options;
  return new ValueData((options ?? []).join(" "));
}

/** The deepest directory of a template that is literal (no bindings); '' if none. */
function literalPrefixDir(template: string): string {
  const prefix = template.split("{")[0];
  if (!prefix.includes("/")) return "";
  return prefix.slice(0, prefix.lastIndexOf("/"));
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
    this.dependencies = opts.dependencies ?? [];
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

    const tscCommand = `{tsc} {options} ${declFlag}--outDir {out_dir} {sources}`;
    if (!this.trackImports) {
      return [
        new ExternalProcessAction({
          name: `tsc -> ${this.outDir}`,
          commandLine: tscCommand,
          inputs,
          outputs,
        }),
      ];
    }

    // Wrap tsc in brix's --listFiles recorder, writing a depfile the incremental
    // layer scans like a C/C++ one. Exclude this module's own out_dir and each
    // declared dependency's directory (those .d.ts are tracked by the ts-dts lens
    // above; byte-tracking them here would defeat the impl-only-change replay).
    const depfile = `${this.outDir}/.brix-tsdeps.d`;
    outputs.depfile = new FileData(depfile);
    inputs.python = this.python;
    const excludeDirs = new Set<string>([this.outDir.replace(/\/+$/, "")]);
    for (const dep of this.dependencies) {
      for (const atom of dep.atoms()) {
        const tmpl =
          atom instanceof GlobData
            ? atom.template
            : atom instanceof FileData
              ? atom.path
              : null;
        if (tmpl !== null) {
          const prefix = literalPrefixDir(tmpl);
          if (prefix) excludeDirs.add(prefix);
        }
      }
    }
    const excludeFlags = [...excludeDirs]
      .sort()
      .map((d) => `--exclude ${d}`)
      .join(" ");
    inputs.excludes = new ValueData(excludeFlags);

    return [
      new ExternalProcessAction({
        name: `tsc -> ${this.outDir}`,
        commandLine: `{python} -m ${WRAPPER_MODULE} {depfile} {excludes} -- ${tscCommand}`,
        inputs,
        outputs,
        depfile,
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
