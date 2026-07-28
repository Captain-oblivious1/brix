/**
 * Typed Rust actions — the TS mirror of `src/brix/rust.py`. Cargo owns
 * within-crate compilation; this skin makes a Cargo build one tracked node in
 * a larger brix graph.
 *
 * Deferred: `cargo()`, which reads Cargo.toml and walks the workspace to infer
 * the artifact and source footprint.
 */

import { Action, EnvValue, ExternalProcessAction } from "./actions";
import { DataExpr, FileData, ToolData, ValueData } from "./data";
import { SYSTEM } from "./env";

// Behaviour-affecting cargo/rustc environment. User entries are spread over
// these defaults in lower(), so an explicit user value wins.
const CARGO_ENV = {
  RUSTFLAGS: SYSTEM,
  RUSTUP_TOOLCHAIN: SYSTEM,
  CARGO_HOME: SYSTEM,
  CARGO_NET_OFFLINE: SYSTEM,
} as const;

export interface CargoBuildOptions {
  manifest: string;
  artifact: string | FileData;
  sources: DataExpr;
  cargo?: ToolData;
  release?: boolean;
  offline?: boolean;
  options?: string[];
  env?: Record<string, EnvValue>;
  envUsed?: string[];
}

export class CargoBuildAction extends Action {
  private readonly manifest: string;
  private readonly artifact: FileData;
  private readonly sources: DataExpr;
  private readonly cargo: ToolData;
  private readonly release: boolean;
  private readonly offline: boolean;
  private readonly options?: string[];
  private readonly env?: Record<string, EnvValue>;
  private readonly envUsed: string[];

  constructor(opts: CargoBuildOptions) {
    super();
    this.manifest = opts.manifest;
    this.artifact =
      opts.artifact instanceof FileData ? opts.artifact : new FileData(opts.artifact);
    this.sources = opts.sources;
    this.cargo = opts.cargo ?? new ToolData("cargo");
    this.release = opts.release ?? true;
    this.offline = opts.offline ?? false;
    this.options = opts.options;
    this.env = opts.env;
    this.envUsed = [...(opts.envUsed ?? [])];
  }

  lower(): Action[] {
    const flags: string[] = [];
    if (this.release) flags.push("--release");
    if (this.offline) flags.push("--offline");
    flags.push(...(this.options ?? []));
    return [
      new ExternalProcessAction({
        name: `cargo build -> ${this.artifact.path}`,
        commandLine: "{cargo} build --manifest-path {manifest} {options}",
        inputs: {
          cargo: this.cargo,
          manifest: new FileData(this.manifest),
          sources: this.sources,
          options: new ValueData(flags.join(" ")),
        },
        outputs: { artifact: this.artifact },
        env: { ...CARGO_ENV, ...(this.env ?? {}) },
        envUsed: this.envUsed,
      }),
    ];
  }
}
