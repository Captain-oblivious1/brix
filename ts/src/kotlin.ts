/**
 * Typed Kotlin actions — the TS mirror of `src/brix/kotlin.py`. This skin
 * provides the kotlin-abi lens and the spawned-kotlinc path of JarAction,
 * including a resolved `classpath=` (a `DepSet` or data expressions): kotlinc
 * wants one path-separator-joined `-classpath` token, so the resolved jar paths
 * are baked into a value, while the same jars ride along as a kotlin-abi-lensed
 * fingerprint input so a dependency change recompiles the module.
 *
 * Deferred: the warm kotlinc worker (needs the shipped-helper asset mechanism).
 */

import { delimiter } from "node:path";

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
import { DepSet } from "./deps";

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

/** Normalize a classpath argument to a list of data expressions: a `DepSet`
 * becomes its byte-lens file atoms (the consumer re-lenses); a list passes
 * through; undefined is empty. Mirrors java's `_as_classpath_list`. */
function asClasspathList(classpath: DepSet | DataExpr[] | undefined): DataExpr[] {
  if (classpath === undefined) return [];
  if (classpath instanceof DepSet) return classpath.files();
  return classpath;
}

/** The FileData paths across all classpath entries (mirrors `_classpath_paths`):
 * kotlinc's `-classpath` takes one path-separator-joined token. */
function classpathPaths(classpath: DataExpr[]): string[] {
  const paths: string[] = [];
  for (const expr of classpath) {
    for (const atom of expr.atoms()) {
      if (atom instanceof FileData) paths.push(atom.path);
    }
  }
  return paths;
}

export interface JarOptions {
  sourceFiles: DataExpr;
  outputFile: FileData;
  kotlinc?: ToolData;
  jarTool?: ToolData;
  classesDir?: string;
  classpath?: DepSet | DataExpr[];
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
  private readonly classpath: DataExpr[];
  private readonly jvmTarget: string;
  private readonly springAllopen?: string;
  private readonly options?: string[] | DataExpr;
  private readonly entryPoint?: string;
  private readonly resourceDir?: string;

  constructor(opts: JarOptions) {
    super();
    if (opts.kotlincWorker) {
      throw new Error(
        "JarAction: kotlincWorker= is not available in the TypeScript frontend " +
          "yet — the warm kotlinc worker needs brix's shipped-helper asset " +
          "mechanism (pending). The spawned-kotlinc path (including classpath=) works.",
      );
    }
    this.sourceFiles = opts.sourceFiles;
    this.outputFile = opts.outputFile;
    this.kotlinc = opts.kotlinc ?? new ToolData("kotlinc");
    this.jarTool = opts.jarTool ?? new ToolData("jar");
    this.classesDir = opts.classesDir ?? "build/kotlin-classes";
    this.classpath = asClasspathList(opts.classpath);
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
    if (this.classpath.length) {
      // kotlinc wants one path-separator-joined -classpath token, so the
      // resolved jar paths are baked into a value; the same jars ride along as
      // a kotlin-abi-lensed fingerprint input (hashed and dependency-inferred,
      // though unreferenced by the template) — kotlin-abi, NOT jvm-abi, because
      // a Kotlin `inline fun` copies its body into this module at compile time.
      compileInputs.classpath = new ValueData(classpathPaths(this.classpath).join(delimiter));
      compileInputs.classpath_track = abi(new DataUnion(...this.classpath));
      compileParts.push("-classpath {classpath}");
    }
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
