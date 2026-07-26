/**
 * Typed Java actions — the TS mirror of `src/brix/java.py`. A language skin:
 * lowers to the generic core (Stage 1), adding no new engine behaviour.
 *
 * This increment: the `jvm-abi` lens (`abi`/`AbiFile`/`AbiGlob`, the analog of
 * ts-dts), `JarAction` (compile + jar), and `JavadocAction`.
 *
 * Gated (see the roadmap): `javac_worker` and `JUnitTestAction` (per-class
 * test-impact) reference brix-shipped helper files — `JavacWorker.java` (a Java
 * source, no module form) and `junit_classload.py` — by path; addressing those
 * portably across frontends is a pending design decision. The resolvers
 * (`pom`/`maven`/`gradle`, `classpath=`) need the shared `DepSet` (deferred).
 */

import { Action, ExternalProcessAction, WorkerSpec } from "./actions";
import {
  DataAtom,
  DataExpr,
  DataUnion,
  FileData,
  GlobData,
  ToolData,
  ValueData,
} from "./data";
import { SYSTEM } from "./env";

// Behaviour-affecting JVM environment, tracked (SYSTEM) so a change re-stales the
// compile — the phase-3 per-language default overlay (docs/environment-recording.md).
const JVM_ENV = {
  JAVA_HOME: SYSTEM,
  JAVA_TOOL_OPTIONS: SYSTEM,
  _JAVA_OPTIONS: SYSTEM,
} as const;

/** A jar/class compared through the JVM ABI lens, not its bytes: a dependent
 *  recompiles on an API change, not an implementation-only one. */
export class AbiFile extends FileData {
  constructor(path: string) {
    super(path);
    this.contentKey = "jvm-abi";
  }
}

export class AbiGlob extends GlobData {
  constructor(template: string, exclude: readonly string[] = []) {
    super(template, exclude);
    this.contentKey = "jvm-abi";
  }
}

/** View an expression through the JVM ABI lens: every plain file/glob atom
 *  becomes its AbiFile/AbiGlob counterpart; other/already-lensed atoms pass
 *  through unchanged. */
export function abi(expr: DataExpr): DataExpr {
  const lensed: DataAtom[] = expr.atoms().map((atom) => {
    if (atom.constructor === FileData) return new AbiFile((atom as FileData).path);
    if (atom.constructor === GlobData) {
      const g = atom as GlobData;
      return new AbiGlob(g.template, [...g.exclude]);
    }
    return atom;
  });
  return lensed.length === 1 ? lensed[0] : new DataUnion(...lensed);
}

function optionsExpr(options: string[] | DataExpr | undefined): DataExpr {
  if (options instanceof DataExpr) return options;
  return new ValueData((options ?? []).join(" "));
}

/** Flatten classpath entries into one ABI-lensed expression (a dependency
 *  recompiles a dependent only on an API change). */
function classpathUnion(classpath: DataExpr[]): DataExpr {
  return abi(new DataUnion(...classpath));
}

export interface JarOptions {
  sourceFiles: DataExpr;
  outputFile: FileData;
  javac?: ToolData;
  jarTool?: ToolData;
  options?: string[] | DataExpr;
  classesDir?: string;
  entryPoint?: string;
  resourceDir?: string;
  // Gated on the javac worker (shipped-helper decision) + shared DepSet.
  classpath?: DataExpr[];
  processorpath?: DataExpr[];
  javacWorker?: WorkerSpec;
}

export class JarAction extends Action {
  private readonly sourceFiles: DataExpr;
  private readonly outputFile: FileData;
  private readonly javac: ToolData;
  private readonly jarTool: ToolData;
  private readonly options?: string[] | DataExpr;
  private readonly classesDir: string;
  private readonly entryPoint?: string;
  private readonly resourceDir?: string;

  constructor(opts: JarOptions) {
    super();
    if (opts.classpath || opts.processorpath || opts.javacWorker) {
      throw new Error(
        "JarAction: classpath=/processorpath=/javacWorker= are not available in " +
          "the TypeScript frontend yet — they need the javac worker (pending the " +
          "shipped-helper decision) and the shared DepSet.",
      );
    }
    this.sourceFiles = opts.sourceFiles;
    this.outputFile = opts.outputFile;
    this.javac = opts.javac ?? new ToolData("javac");
    this.jarTool = opts.jarTool ?? new ToolData("jar");
    this.options = opts.options;
    this.classesDir = opts.classesDir ?? "build/classes";
    this.entryPoint = opts.entryPoint;
    this.resourceDir = opts.resourceDir;
  }

  lower(): Action[] {
    // Bare `{name}` is recursive, so class tracking reaches packaged classes
    // (com/foo/Bar.class), not just default-package ones.
    const classes = new GlobData(`${this.classesDir}/{name}.class`);
    const compileJava = new ExternalProcessAction({
      name: `javac -> ${this.classesDir}`,
      commandLine: "{javac} {options} -d {classes_dir} {sources}",
      inputs: {
        javac: this.javac,
        options: optionsExpr(this.options),
        sources: this.sourceFiles,
      },
      outputs: {
        classes_dir: new FileData(this.classesDir),
        classes,
      },
      env: JVM_ENV,
    });

    let jarCommand: string;
    const jarInputs: Record<string, DataExpr> = {
      jar: this.jarTool,
      classes_dir: new FileData(this.classesDir),
      classes, // recursive glob — reruns on any class change
    };
    if (this.entryPoint) {
      jarCommand = "{jar} cfe {output} {entry_point} -C {classes_dir} .";
      jarInputs.entry_point = new ValueData(this.entryPoint);
    } else {
      jarCommand = "{jar} cf {output} -C {classes_dir} .";
    }
    if (this.resourceDir !== undefined) {
      // A second `-C <root> .` packs the resource tree at its relative paths.
      jarCommand += " -C {resource_dir} .";
      jarInputs.resource_dir = new FileData(this.resourceDir);
      jarInputs.resources = new GlobData(`${this.resourceDir}/{name}`);
    }
    const pkg = new ExternalProcessAction({
      name: `jar ${this.outputFile.path}`,
      commandLine: jarCommand,
      inputs: jarInputs,
      outputs: { output: this.outputFile },
    });
    return [compileJava, pkg];
  }
}

export interface JavadocOptions {
  sourceFiles: DataExpr;
  outputDir: string;
  javadoc?: ToolData;
  options?: string[] | DataExpr;
  /** Resolved deps, ABI-lensed (impl-only change → docs replay). DepSet deferred. */
  classpath?: DataExpr[];
}

export class JavadocAction extends Action {
  private readonly sourceFiles: DataExpr;
  private readonly outputDir: string;
  private readonly javadoc: ToolData;
  private readonly options?: string[] | DataExpr;
  private readonly classpath: DataExpr[];

  constructor(opts: JavadocOptions) {
    super();
    this.sourceFiles = opts.sourceFiles;
    this.outputDir = opts.outputDir;
    this.javadoc = opts.javadoc ?? new ToolData("javadoc");
    this.options = opts.options;
    this.classpath = opts.classpath ?? [];
  }

  lower(): Action[] {
    const inputs: Record<string, DataExpr> = {
      javadoc: this.javadoc,
      options: optionsExpr(this.options),
      sources: this.sourceFiles,
    };
    const parts = ["{javadoc}", "-d {output_dir}"];
    if (this.classpath.length) {
      inputs.classpath = classpathUnion(this.classpath);
      parts.push("-classpath {classpath}");
    }
    parts.push("{options}", "{sources}");
    return [
      new ExternalProcessAction({
        name: `javadoc -> ${this.outputDir}`,
        commandLine: parts.join(" "),
        inputs,
        outputs: {
          output_dir: new FileData(this.outputDir),
          files: new GlobData(`${this.outputDir}/{name}`),
        },
      }),
    ];
  }
}
