/**
 * Typed Kotlin actions — the TS mirror of `src/brix/kotlin.py`. This skin
 * provides the kotlin-abi lens and the spawned-kotlinc path of JarAction.
 *
 * Deferred: the classpath/DepSet path and warm kotlinc worker, both of which
 * depend on frontend support that is not available yet.
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

const SPRING_PRESET = "plugin:org.jetbrains.kotlin.allopen:preset=spring";

export class KotlinAbiFile extends FileData {
  constructor(path: string) {
    super(path);
    this.contentKey = "kotlin-abi";
  }
}

export class KotlinAbiGlob extends GlobData {
  constructor(template: string, exclude: readonly string[] = []) {
    super(template, exclude);
    this.contentKey = "kotlin-abi";
  }
}

/** View plain file/glob atoms through the Kotlin ABI lens. */
export function abi(expr: DataExpr): DataExpr {
  const lensed: DataAtom[] = expr.atoms().map((atom) => {
    if (atom.constructor === FileData) {
      return new KotlinAbiFile((atom as FileData).path);
    }
    if (atom.constructor === GlobData) {
      const glob = atom as GlobData;
      return new KotlinAbiGlob(glob.template, [...glob.exclude]);
    }
    return atom;
  });
  return lensed.length === 1 ? lensed[0] : new DataUnion(...lensed);
}

function optionsExpr(options: string[] | DataExpr | undefined): DataExpr {
  if (options instanceof DataExpr) return options;
  return new ValueData((options ?? []).join(" "));
}

export interface JarOptions {
  sourceFiles: DataExpr;
  outputFile: FileData;
  kotlinc?: ToolData;
  jarTool?: ToolData;
  classesDir?: string;
  classpath?: DataExpr[];
  jvmTarget?: string;
  springAllopen?: string;
  options?: string[] | DataExpr;
  entryPoint?: string;
  resourceDir?: string;
  kotlincWorker?: WorkerSpec;
}

export class JarAction extends Action {
  private readonly sourceFiles: DataExpr;
  private readonly outputFile: FileData;
  private readonly kotlinc: ToolData;
  private readonly jarTool: ToolData;
  private readonly classesDir: string;
  private readonly jvmTarget: string;
  private readonly springAllopen?: string;
  private readonly options?: string[] | DataExpr;
  private readonly entryPoint?: string;
  private readonly resourceDir?: string;

  constructor(opts: JarOptions) {
    super();
    if (opts.classpath || opts.kotlincWorker) {
      throw new Error(
        "JarAction: classpath=/kotlincWorker= are not available in the TypeScript " +
          "frontend yet — they need the kotlinc worker and the shared DepSet.",
      );
    }
    this.sourceFiles = opts.sourceFiles;
    this.outputFile = opts.outputFile;
    this.kotlinc = opts.kotlinc ?? new ToolData("kotlinc");
    this.jarTool = opts.jarTool ?? new ToolData("jar");
    this.classesDir = opts.classesDir ?? "build/kotlin-classes";
    this.jvmTarget = opts.jvmTarget ?? "17";
    this.springAllopen = opts.springAllopen;
    this.options = opts.options;
    this.entryPoint = opts.entryPoint;
    this.resourceDir = opts.resourceDir;
  }

  lower(): Action[] {
    const classes = new GlobData(`${this.classesDir}/{name}.class`);
    const compileInputs: Record<string, DataExpr> = {
      kotlinc: this.kotlinc,
      jvm_target: new ValueData(this.jvmTarget),
      options: optionsExpr(this.options),
    };
    const compileParts = ["{kotlinc}", "-jvm-target {jvm_target}"];
    if (this.springAllopen !== undefined) {
      compileInputs.allopen = new FileData(this.springAllopen);
      compileInputs.preset = new ValueData(SPRING_PRESET);
      compileParts.push("-Xplugin={allopen} -P {preset}");
    }
    compileParts.push("{options}", "-d {classes_dir}", "{sources}");
    compileInputs.sources = this.sourceFiles;
    const compileKotlin = new ExternalProcessAction({
      name: `kotlinc -> ${this.classesDir}`,
      commandLine: compileParts.join(" "),
      inputs: compileInputs,
      outputs: {
        classes_dir: new FileData(this.classesDir),
        classes,
      },
    });

    let jarCommand: string;
    const jarInputs: Record<string, DataExpr> = {
      jar: this.jarTool,
      classes_dir: new FileData(this.classesDir),
      classes,
    };
    if (this.entryPoint) {
      jarCommand = "{jar} cfe {output} {entry_point} -C {classes_dir} .";
      jarInputs.entry_point = new ValueData(this.entryPoint);
    } else {
      jarCommand = "{jar} cf {output} -C {classes_dir} .";
    }
    if (this.resourceDir !== undefined) {
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
    return [compileKotlin, pkg];
  }
}
