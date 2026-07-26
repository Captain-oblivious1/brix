/**
 * Typed Python actions — the TS mirror of `src/brix/python.py`. The first
 * language *skin*: it lowers to the generic core proven in Stage 1, adding no
 * new engine behaviour.
 *
 * `PytestAction` runs a project's pytest suite with per-test-file impact
 * selection by import discovery — the Python member of brix's test-impact family
 * (Java `-Xlog:class+load`, C++ `-MMD`, TS imports).
 *
 * Deviation from Python: `python` has no default here. Python defaults to
 * `sys.executable` (the interpreter running the build); Node is running the TS
 * build, which is not a Python interpreter, so the interpreter must be named.
 */

import { Action, ExpandAll, ExternalProcessAction } from "./actions";
import { DataExpr, FileData, GlobData, ToolData, ValueData } from "./data";

// brix's pytest plugin: at session end it writes a brix JSON dependency manifest
// of the project modules the run imported. Loaded with `-p`; requires brix to be
// importable by the interpreter running pytest (installed, or env=PYTHONPATH).
const PLUGIN = "brix._python.pytest_brixdeps";

function singleBinding(collection: GlobData, what: string): string {
  const names = collection.bindingNames();
  if (names.length !== 1) {
    throw new Error(
      `${what} must have exactly one {binding}, got ${JSON.stringify(collection.template)}`,
    );
  }
  return names[0];
}

function optionsExpr(options: string[] | DataExpr | undefined): DataExpr {
  if (options instanceof DataExpr) return options;
  return new ValueData((options ?? []).join(" "));
}

export interface PytestOptions {
  /** A glob with exactly one `{binding}`, e.g. `tests/test_{name}.py`. */
  testSources: GlobData;
  /** The interpreter (required — brix must be importable by it). */
  python: ToolData;
  manifestDir?: string;
  options?: string[] | DataExpr;
  env?: Record<string, string>;
}

export class PytestAction extends Action {
  private readonly testSources: GlobData;
  private readonly python: ToolData;
  private readonly manifestDir: string;
  private readonly options?: string[] | DataExpr;
  private readonly env: Record<string, string>;
  private readonly binding: string;

  constructor(opts: PytestOptions) {
    super();
    this.testSources = opts.testSources;
    this.python = opts.python;
    this.manifestDir = (opts.manifestDir ?? "build/.brix/pytest").replace(/\/+$/, "");
    this.options = opts.options;
    this.env = { ...(opts.env ?? {}) };
    this.binding = singleBinding(opts.testSources, "testSources");
  }

  lower(): Action[] {
    const name = this.binding;
    const manifest = `${this.manifestDir}/{${name}}.json`;
    const runOne = new ExternalProcessAction({
      name: `pytest {${name}}`,
      commandLine:
        `{python} -m pytest {testfile}` +
        ` -p ${PLUGIN} --brix-manifest {manifest} {options}`,
      inputs: {
        python: this.python,
        testfile: new FileData(this.testSources.template),
        options: optionsExpr(this.options),
      },
      // The manifest is the only file output; the verdict is the exit code,
      // replayed on the fingerprint. A failed run drops it and re-runs.
      outputs: { manifest: new FileData(manifest) },
      depfile: manifest,
      env: this.env,
    });
    return [
      new ExpandAll({ elementName: name, collection: this.testSources, child: runOne }),
    ];
  }
}
