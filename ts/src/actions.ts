/**
 * Logical actions — the TS mirror of `src/brix/actions.py`.
 *
 * Actions consume named data expressions and produce named data expressions.
 * They are clean-build truth; build capabilities (incremental, cache, atomic)
 * are applied by planning layers, never declared here. Pure field-holders: the
 * TS side only lowers to the wire spec — expansion and substitution happen in
 * the Rust core.
 *
 * Deferred (an env-isolation follow-on, faithful to add later): the
 * `SYSTEM`/`PASS`/`UNSET` env sentinels and `envUsed` snapshotting. Stage-1
 * core supports `env` as ordinary set values.
 */

import { DataExpr, ValueData } from "./data";

export abstract class Action {}

/** Anything an ExpandAll can instantiate per collection member. */
export abstract class Expandable extends Action {}

/** Routes an action's argv to a persistent worker instead of spawning it. */
export interface WorkerSpec {
  name: string;
  commandLine: string;
  inputs: Record<string, DataExpr>;
}

export interface ExternalProcessOptions {
  name?: string;
  commandLine: string;
  inputs?: Record<string, DataExpr>;
  outputs?: Record<string, DataExpr>;
  /** Set values for the process environment; a plain string is a literal. */
  env?: Record<string, DataExpr | string>;
  /** Logical path of a Makefile/JSON depfile this action emits (folded in). */
  depfile?: string;
  worker?: WorkerSpec;
}

export class ExternalProcessAction extends Expandable {
  readonly name?: string;
  readonly commandLine: string;
  readonly inputs: Record<string, DataExpr>;
  readonly outputs: Record<string, DataExpr>;
  readonly env: Record<string, DataExpr>;
  readonly depfile?: string;
  readonly worker?: WorkerSpec;

  constructor(opts: ExternalProcessOptions) {
    super();
    this.name = opts.name;
    this.commandLine = opts.commandLine;
    this.inputs = { ...(opts.inputs ?? {}) };
    this.outputs = { ...(opts.outputs ?? {}) };
    this.env = {};
    for (const [k, v] of Object.entries(opts.env ?? {})) {
      this.env[k] = typeof v === "string" ? new ValueData(v) : v;
    }
    this.depfile = opts.depfile;
    this.worker = opts.worker;
  }
}

export interface ExpandAllOptions {
  elementName: string;
  collection: DataExpr;
  child: Expandable;
}

export class ExpandAll extends Expandable {
  readonly elementName: string;
  readonly collection: DataExpr;
  readonly child: Expandable;

  constructor(opts: ExpandAllOptions) {
    super();
    if (!(opts.child instanceof Expandable)) {
      throw new TypeError(
        "ExpandAll child must be Expandable (ExternalProcessAction or ExpandAll)",
      );
    }
    this.elementName = opts.elementName;
    this.collection = opts.collection;
    this.child = opts.child;
  }
}
