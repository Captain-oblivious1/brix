/**
 * Builders — the TS mirror of `src/brix/builders.py`.
 *
 * `CleanBuilder` and `IncrementalBuilder` are root builders; `CacheBuilder` and
 * `AtomicBuilder` wrap another builder, each contributing a layer to the stack:
 *
 *     new AtomicBuilder(new CacheBuilder(new IncrementalBuilder(createGraph)))
 *
 * cache innermost, incremental around it, atomic outermost. `AtomicBuilder` is
 * intentionally not nestable — it must be outermost (its staging overlay and
 * commit/discard workflow) — enforced here and, language-neutrally, in the core.
 *
 * Deferred (Stage 1 core): a custom `store` object on CacheBuilder, and the
 * git/verify-contents/scrub detector knobs. `cacheDir` (the built-in store) and
 * the plain stack are here.
 */

import * as path from "path";

import { Graph } from "./graph";
import { LayerSpec, RunBuildOptions, runBuild } from "./engine";
import { Reporter } from "./report";
import { BuildResult } from "./results";

export type GraphSupplier = Graph | (() => Graph);

function asSupplier(g: GraphSupplier): () => Graph {
  return typeof g === "function" ? g : () => g;
}

export interface BuildOptions {
  jobs?: number | null;
  reporter?: Reporter;
  targets?: string[] | null;
  workerIdleTimeout?: number;
}

/** A builder another layer may legally wrap. `AtomicBuilder` is not one. */
export interface NestableBuilder {
  readonly root: string;
  sourceGraph(): Graph;
  layerSpecs(): LayerSpec[];
  build(opts?: BuildOptions): Promise<BuildResult>;
}

function rejectWrappedAtomic(inner: unknown): void {
  if (inner instanceof AtomicBuilder) {
    throw new TypeError(
      "AtomicBuilder must be the outermost builder — write " +
        "new AtomicBuilder(new CacheBuilder(...)), not the reverse.",
    );
  }
}

export class CleanBuilder implements NestableBuilder {
  readonly root: string;
  private supplier: () => Graph;

  constructor(graph: GraphSupplier, root = ".") {
    this.supplier = asSupplier(graph);
    this.root = path.resolve(root);
  }

  sourceGraph(): Graph {
    return this.supplier();
  }

  layerSpecs(): LayerSpec[] {
    return [];
  }

  build(opts: BuildOptions = {}): Promise<BuildResult> {
    return runBuild(this.sourceGraph(), buildOpts(this.root, this.layerSpecs(), opts));
  }
}

export interface IncrementalOptions {
  root?: string;
  metadataDir?: string;
}

export class IncrementalBuilder implements NestableBuilder {
  readonly root: string;
  readonly metadataDir: string;
  private supplier: () => Graph;

  constructor(graph: GraphSupplier, opts: IncrementalOptions = {}) {
    this.supplier = asSupplier(graph);
    this.root = path.resolve(opts.root ?? ".");
    this.metadataDir = opts.metadataDir ?? "build/.brix";
  }

  sourceGraph(): Graph {
    return this.supplier();
  }

  layerSpecs(): LayerSpec[] {
    return [{ kind: "incremental", metadata_dir: this.metadataDir }];
  }

  build(opts: BuildOptions = {}): Promise<BuildResult> {
    return runBuild(this.sourceGraph(), buildOpts(this.root, this.layerSpecs(), opts));
  }
}

export interface CacheOptions {
  cacheDir?: string;
}

export class CacheBuilder implements NestableBuilder {
  readonly root: string;
  readonly cacheDir: string;

  constructor(private inner: NestableBuilder, opts: CacheOptions = {}) {
    rejectWrappedAtomic(inner);
    this.root = inner.root;
    this.cacheDir = opts.cacheDir ?? "build/.brix-cache";
  }

  sourceGraph(): Graph {
    return this.inner.sourceGraph();
  }

  layerSpecs(): LayerSpec[] {
    return [{ kind: "cache", cache_dir: this.cacheDir }, ...this.inner.layerSpecs()];
  }

  build(opts: BuildOptions = {}): Promise<BuildResult> {
    return runBuild(this.sourceGraph(), buildOpts(this.root, this.layerSpecs(), opts));
  }
}

export interface AtomicOptions {
  stagingDir?: string;
}

export class AtomicBuilder {
  readonly root: string;
  readonly stagingDir: string;

  constructor(private inner: NestableBuilder, opts: AtomicOptions = {}) {
    rejectWrappedAtomic(inner);
    this.root = inner.root;
    this.stagingDir = opts.stagingDir ?? "build/staging";
  }

  sourceGraph(): Graph {
    return this.inner.sourceGraph();
  }

  layerSpecs(hold = false): LayerSpec[] {
    return [
      ...this.inner.layerSpecs(),
      { kind: "atomic", staging_dir: this.stagingDir, hold },
    ];
  }

  build(opts: BuildOptions & { hold?: boolean } = {}): Promise<BuildResult> {
    return runBuild(
      this.sourceGraph(),
      buildOpts(this.root, this.layerSpecs(opts.hold ?? false), opts),
    );
  }
}

function buildOpts(
  root: string,
  layers: LayerSpec[],
  opts: BuildOptions,
): RunBuildOptions {
  return {
    root,
    layers,
    jobs: opts.jobs ?? null,
    targets: opts.targets ?? null,
    reporter: opts.reporter,
    workerIdleTimeout: opts.workerIdleTimeout,
  };
}

export interface BuildStackOptions {
  root?: string;
  metadataDir?: string;
  cacheDir?: string | null;
  stagingDir?: string | null;
}

/** Assemble the standard stack in the correct order (atomic outermost). */
export function buildStack(
  graph: GraphSupplier,
  opts: BuildStackOptions = {},
): AtomicBuilder | NestableBuilder {
  const root = opts.root ?? ".";
  let inner: NestableBuilder = new IncrementalBuilder(graph, {
    root,
    metadataDir: opts.metadataDir,
  });
  const cacheDir = opts.cacheDir === undefined ? "build/.brix-cache" : opts.cacheDir;
  if (cacheDir !== null) inner = new CacheBuilder(inner, { cacheDir });
  const stagingDir = opts.stagingDir === undefined ? "build/staging" : opts.stagingDir;
  if (stagingDir !== null) return new AtomicBuilder(inner, { stagingDir });
  return inner;
}
