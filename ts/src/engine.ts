/**
 * The engine boundary — the TS mirror of `src/brix/engine.py`.
 *
 * Assembles the JSON config, calls the native addon, and decodes the result.
 * `build` is async (deviation #2): the Promise resolves to the BuildResult once
 * the Rust engine finishes; events arrive live on the JS loop meanwhile and are
 * replayed onto the reporter, then `buildEnd` is synthesized from the returned
 * result (which carries the final plan the event stream's copy would not).
 *
 * Deferred (Stage 1 core): custom cache-store / content-identity callbacks —
 * the optional extension traits. A build that uses only the default filesystem
 * cache and built-in identities needs none of that.
 */

import { Graph } from "./graph";
import { graphToSpec } from "./lowering";
import { native } from "./native";
import { Reporter, dispatchEvent } from "./report";
import { BuildResult, decodeBuildResult } from "./results";

/** A layer config entry, innermost-first (docs/boundary-schema.md). */
export type LayerSpec = Record<string, unknown>;

export interface RunBuildOptions {
  root: string;
  layers: LayerSpec[];
  jobs?: number | null;
  targets?: string[] | null;
  reporter?: Reporter;
  workerIdleTimeout?: number;
  workerd?: string;
}

export async function runBuild(
  graph: Graph,
  opts: RunBuildOptions,
): Promise<BuildResult> {
  const specJson = JSON.stringify(graphToSpec(graph));
  const idle = opts.workerIdleTimeout ?? 900;
  const config: Record<string, unknown> = {
    root: opts.root,
    jobs: opts.jobs ?? null,
    targets: opts.targets ?? null,
    layers: opts.layers,
    worker_idle_timeout: idle,
  };
  if (idle > 0) config.workerd = opts.workerd ?? "brix-workerd";
  const configJson = JSON.stringify(config);

  const onEvent = opts.reporter
    ? (json: string) => dispatchEvent(opts.reporter!, json)
    : undefined;
  const result = decodeBuildResult(await native.build(specJson, configJson, onEvent));
  opts.reporter?.buildEnd?.(result);
  return result;
}

export interface StatusReport {
  actions: {
    label: string;
    state: string;
    detail: string;
    last_result: unknown | null;
  }[];
  pending_deletions: unknown[];
  staged_files: string[];
  staged_deletions: string[];
  interrupted_commit: boolean;
  held: boolean;
}

export function runStatus(
  graph: Graph,
  opts: { root: string; metadataDir: string; stagingDir: string; targets?: string[] | null },
): StatusReport {
  const config = {
    root: opts.root,
    metadata_dir: opts.metadataDir,
    staging_dir: opts.stagingDir,
    targets: opts.targets ?? null,
  };
  return JSON.parse(native.status(JSON.stringify(graphToSpec(graph)), JSON.stringify(config)));
}

export function runClean(
  graph: Graph,
  opts: { root: string; metadataDir: string },
): number {
  const config = { root: opts.root, metadata_dir: opts.metadataDir };
  const raw = JSON.parse(native.clean(JSON.stringify(graphToSpec(graph)), JSON.stringify(config)));
  return raw.removed as number;
}
