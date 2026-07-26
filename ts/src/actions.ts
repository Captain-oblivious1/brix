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
import { EnvSentinel, PASS, SYSTEM, UNSET } from "./env";

export abstract class Action {}

/** Anything an ExpandAll can instantiate per collection member. */
export abstract class Expandable extends Action {}

/** Routes an action's argv to a persistent worker instead of spawning it. */
export interface WorkerSpec {
  name: string;
  commandLine: string;
  inputs: Record<string, DataExpr>;
}

/** An `env` value: a literal (set + fingerprint), a DataExpr, or a sentinel. */
export type EnvValue = DataExpr | string | EnvSentinel;

// The input key `env_used=`/SYSTEM materializes into — reserved (brix-namespaced)
// so it cannot collide with an author's key and is never a command placeholder.
const ENV_USED_KEY = "_brix_env_used";

/** Python-compatible JSON of a flat {name: string|null} map: sorted keys, and
 *  `json.dumps`'s `", "`/`": "` separators, so the snapshot byte-matches the
 *  Python skin's (it is fingerprint material). */
function pyJson(obj: Record<string, string | null>): string {
  const keys = Object.keys(obj).sort();
  return (
    "{" +
    keys.map((k) => `${JSON.stringify(k)}: ${JSON.stringify(obj[k])}`).join(", ") +
    "}"
  );
}

/** Snapshot the named ambient variables into one fingerprint-only ValueData —
 *  the env_used guard (docs/environment-recording.md). Unset → null (kept
 *  distinct from set-empty). Mirrors Python's `_resolve_env_used`. */
function resolveEnvUsed(names: string[]): DataExpr {
  const snapshot: Record<string, string | null> = {};
  for (const name of [...new Set(names)].sort()) {
    snapshot[name] = process.env[name] ?? null;
  }
  return new ValueData(pyJson(snapshot));
}

export interface ExternalProcessOptions {
  name?: string;
  commandLine: string;
  inputs?: Record<string, DataExpr>;
  outputs?: Record<string, DataExpr>;
  /** Set values / sentinels for the process environment; a plain string is a literal. */
  env?: Record<string, EnvValue>;
  /** Ambient variables to fingerprint without setting (like SYSTEM values). */
  envUsed?: string[];
  /** Variables to force absent from the process environment. */
  envUnset?: string[];
  /** Variables to let through under scrub without fingerprinting. */
  envPass?: string[];
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
  readonly envUnset: string[];
  readonly envPass: string[];
  readonly envUsed: string[];
  readonly depfile?: string;
  readonly worker?: WorkerSpec;

  constructor(opts: ExternalProcessOptions) {
    super();
    this.name = opts.name;
    this.commandLine = opts.commandLine;
    this.inputs = { ...(opts.inputs ?? {}) };
    this.outputs = { ...(opts.outputs ?? {}) };
    if (ENV_USED_KEY in this.inputs) {
      throw new Error(
        `input key '${ENV_USED_KEY}' is reserved: it is materialized by envUsed=/SYSTEM, never supplied directly`,
      );
    }

    // Partition env by sentinel intent, exactly like the Python action.
    const systemTracked: string[] = [];
    const unsetKeys: string[] = [];
    const passKeys: string[] = [];
    this.env = {};
    for (const [k, v] of Object.entries(opts.env ?? {})) {
      if (v === SYSTEM) systemTracked.push(k);
      else if (v === UNSET) unsetKeys.push(k);
      else if (v === PASS) passKeys.push(k);
      else this.env[k] = v instanceof DataExpr ? v : new ValueData(v as string);
    }
    this.envUnset = [...new Set([...unsetKeys, ...(opts.envUnset ?? [])])].sort();
    this.envPass = [...new Set([...passKeys, ...(opts.envPass ?? [])])].sort();
    // Order-preserving de-dup (SYSTEM folds in after explicit envUsed=).
    this.envUsed = [...new Set([...(opts.envUsed ?? []), ...systemTracked])];
    if (this.envUsed.length) this.inputs[ENV_USED_KEY] = resolveEnvUsed(this.envUsed);

    this.depfile = opts.depfile;
    this.worker = opts.worker;
    // A warm worker's environment is fixed at launch, so per-action set/unset
    // can never reach it — fail loud rather than fingerprint an env the process
    // never saw. Tracking (SYSTEM) and PASS are fingerprint/scrub-side only.
    if (this.worker && (Object.keys(this.env).length || this.envUnset.length)) {
      const pinned = [...new Set([...Object.keys(this.env), ...this.envUnset])].sort();
      throw new Error(
        `worker-routed action '${this.name ?? this.commandLine}' cannot set or unset ` +
          `environment variables ${JSON.stringify(pinned)}: a warm worker's environment ` +
          `is fixed at launch; use SYSTEM to track, or drop worker=`,
      );
    }
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
