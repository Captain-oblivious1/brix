/**
 * Typed C++ actions — the TS mirror of `src/brix/cpp.py`. A language skin:
 * lowers to the generic core (Stage 1), adding no new engine behaviour.
 *
 * Covers CompileSourceAction (per-TU compile with `-MMD` header tracking),
 * LinkApplicationAction, ArchiveAction, RunTestAction (per-binary impact
 * selection), and DoxygenAction. No content lens, no shipped helper.
 *
 * `deps=` takes a `DepSet`: its `compileFlags` (`-I`/`-D`) join every TU's
 * compile command, its `linkFlags` (`-L`/`-l`) and artifact files (byte-lens
 * tracked) go on the link *after* the objects (link order is load-bearing).
 * Deferred (needs external tools): the `pkg_config` resolver that produces one.
 */

import { Action, ExpandAll, ExternalProcessAction } from "./actions";
import {
  DataExpr,
  DataUnion,
  FileData,
  GlobData,
  ToolData,
  ValueData,
} from "./data";
import { DepSet } from "./deps";
import { SYSTEM } from "./env";

// Behaviour-affecting C/C++ toolchain environment, tracked (SYSTEM) so a change
// re-stales the compile/link (docs/environment-recording.md).
const TOOLCHAIN_ENV = {
  CPATH: SYSTEM,
  C_INCLUDE_PATH: SYSTEM,
  CPLUS_INCLUDE_PATH: SYSTEM,
  LIBRARY_PATH: SYSTEM,
  SOURCE_DATE_EPOCH: SYSTEM,
} as const;

function optionsExpr(options: string[] | DataExpr | undefined): DataExpr {
  if (options instanceof DataExpr) return options;
  return new ValueData((options ?? []).join(" "));
}

function singleBinding(collection: GlobData, what: string): string {
  const names = collection.bindingNames();
  if (names.length !== 1) {
    throw new Error(
      `${what} must have exactly one {binding}, got ${JSON.stringify(collection.template)}`,
    );
  }
  return names[0];
}

export interface CompileSourceOptions {
  source_file: GlobData;
  object_file: GlobData;
  compiler?: ToolData;
  compile_options?: string[] | DataExpr;
  deps?: DepSet;
}

export class CompileSourceAction extends Action {
  private readonly sourceFile: GlobData;
  private readonly objectFile: GlobData;
  private readonly compiler: ToolData;
  private readonly compileOptions?: string[] | DataExpr;
  private readonly deps?: DepSet;
  private readonly binding: string;

  constructor(opts: CompileSourceOptions) {
    super();
    const sourceBinding = singleBinding(opts.source_file, "source_file");
    const objectBinding = singleBinding(opts.object_file, "object_file");
    if (sourceBinding !== objectBinding) {
      throw new Error(
        `source_file binds {${sourceBinding}} but object_file binds {${objectBinding}}; they must match`,
      );
    }
    this.sourceFile = opts.source_file;
    this.objectFile = opts.object_file;
    this.compiler = opts.compiler ?? new ToolData("c++");
    this.compileOptions = opts.compile_options;
    // A DepSet's compile flags (-I/-D from e.g. cpp.pkg_config) are added to
    // every translation unit's command as a separate value, so they compose
    // with any compile_options form and re-stale the compile when they change.
    this.deps = opts.deps;
    this.binding = sourceBinding;
  }

  lower(): Action[] {
    const name = this.binding;
    const objTemplate = this.objectFile.template;
    const depfile = `${objTemplate}.d`;
    const depCflags = this.deps ? this.deps.compileFlags.join(" ") : "";
    return [
      new ExpandAll({
        elementName: name,
        collection: this.sourceFile,
        child: new ExternalProcessAction({
          name: `compile {${name}}`,
          // -MMD writes user-header prerequisites; -MF puts the depfile where
          // the incremental layer scans it, so editing an #included header
          // re-stales this compile.
          commandLine:
            "{compiler} {options} {dep_cflags} -MMD -MF {depfile} -c {source} -o {object}",
          inputs: {
            compiler: this.compiler,
            options: optionsExpr(this.compileOptions),
            dep_cflags: new ValueData(depCflags),
            source: new FileData(this.sourceFile.template),
          },
          outputs: {
            object: new FileData(objTemplate),
            depfile: new FileData(depfile),
          },
          depfile,
          env: TOOLCHAIN_ENV,
        }),
      }),
    ];
  }
}

export interface LinkApplicationOptions {
  object_files: DataExpr;
  output_file: FileData;
  linker?: ToolData;
  link_options?: string[] | DataExpr;
  deps?: DepSet;
}

export class LinkApplicationAction extends Action {
  private readonly objectFiles: DataExpr;
  private readonly outputFile: FileData;
  private readonly linker: ToolData;
  private readonly linkOptions?: string[] | DataExpr;
  private readonly deps?: DepSet;

  constructor(opts: LinkApplicationOptions) {
    super();
    this.objectFiles = opts.object_files;
    this.outputFile = opts.output_file;
    this.linker = opts.linker ?? new ToolData("c++");
    this.linkOptions = opts.link_options;
    // A DepSet's link artifacts (.a/.so, tracked as byte-lens inputs) and link
    // flags (-L/-l) go *after* the objects — link order is load-bearing.
    this.deps = opts.deps;
  }

  lower(): Action[] {
    const depLibs = new DataUnion(...(this.deps ? this.deps.files() : []));
    const depLflags = this.deps ? this.deps.linkFlags.join(" ") : "";
    return [
      new ExternalProcessAction({
        name: `link ${this.outputFile.path}`,
        commandLine: "{linker} {options} {objects} {dep_libs} {dep_lflags} -o {output}",
        inputs: {
          linker: this.linker,
          options: optionsExpr(this.linkOptions),
          objects: this.objectFiles,
          dep_libs: depLibs, // tracked artifact files (empty for pkg-config)
          dep_lflags: new ValueData(depLflags),
        },
        outputs: { output: this.outputFile },
        env: TOOLCHAIN_ENV,
      }),
    ];
  }
}

export interface ArchiveOptions {
  object_files: DataExpr;
  output_file: FileData;
  ar?: ToolData;
}

export class ArchiveAction extends Action {
  private readonly objectFiles: DataExpr;
  private readonly outputFile: FileData;
  private readonly ar: ToolData;

  constructor(opts: ArchiveOptions) {
    super();
    this.objectFiles = opts.object_files;
    this.outputFile = opts.output_file;
    this.ar = opts.ar ?? new ToolData("ar");
  }

  lower(): Action[] {
    return [
      new ExternalProcessAction({
        name: `ar ${this.outputFile.path}`,
        commandLine: "{ar} Dcrs {output} {objects}",
        inputs: { ar: this.ar, objects: this.objectFiles },
        outputs: { output: this.outputFile },
      }),
    ];
  }
}

export interface RunTestOptions {
  test_binary: string | FileData;
  name?: string;
  args?: string[] | DataExpr;
  runtime_inputs?: (string | DataExpr)[];
  runner?: ToolData;
}

export class RunTestAction extends Action {
  private readonly testBinary: FileData;
  private readonly name: string;
  private readonly args?: string[] | DataExpr;
  private readonly runtimeInputs: (string | DataExpr)[];
  private readonly runner?: ToolData;

  constructor(opts: RunTestOptions) {
    super();
    this.testBinary =
      opts.test_binary instanceof FileData ? opts.test_binary : new FileData(opts.test_binary);
    this.name = opts.name ?? `run ${this.testBinary.path}`;
    this.args = opts.args;
    this.runtimeInputs = opts.runtime_inputs ?? [];
    this.runner = opts.runner;
  }

  private binaryRef(): string {
    // Exec'd (not shell'd): argv[0]. A path with a separator runs as a path on
    // every OS; only a bare name in the cwd needs POSIX `./`, never on Windows.
    const path = this.testBinary.path;
    if (path.includes("/") || path.includes("\\") || process.platform === "win32") {
      return "{binary}";
    }
    return "./{binary}";
  }

  lower(): Action[] {
    const inputs: Record<string, DataExpr> = { binary: this.testBinary };
    this.runtimeInputs.forEach((extra, i) => {
      inputs[`runtime${i}`] = extra instanceof DataExpr ? extra : new FileData(extra);
    });
    inputs.args = optionsExpr(this.args);
    let command: string;
    if (this.runner !== undefined) {
      inputs.runner = this.runner;
      command = "{runner} {binary} {args}";
    } else {
      command = `${this.binaryRef()} {args}`;
    }
    return [
      new ExternalProcessAction({
        name: this.name,
        commandLine: command,
        inputs,
        outputs: {}, // exit code only — replays on the input fingerprint
      }),
    ];
  }
}

export interface DoxygenOptions {
  config: string;
  source_files: DataExpr;
  output_dir: string;
  doxygen?: ToolData;
}

export class DoxygenAction extends Action {
  private readonly config: string;
  private readonly sourceFiles: DataExpr;
  private readonly outputDir: string;
  private readonly doxygen: ToolData;

  constructor(opts: DoxygenOptions) {
    super();
    this.config = opts.config;
    this.sourceFiles = opts.source_files;
    this.outputDir = opts.output_dir;
    this.doxygen = opts.doxygen ?? new ToolData("doxygen");
  }

  lower(): Action[] {
    return [
      new ExternalProcessAction({
        name: `doxygen -> ${this.outputDir}`,
        commandLine: "{doxygen} {config}",
        inputs: {
          doxygen: this.doxygen,
          config: new FileData(this.config),
          sources: this.sourceFiles,
        },
        outputs: {
          output_dir: new FileData(this.outputDir),
          files: new GlobData(`${this.outputDir}/{name}`),
        },
      }),
    ];
  }
}
