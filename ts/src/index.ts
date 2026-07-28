/**
 * Brix authoring API in TypeScript — the Python API, transliterated
 * (docs/boundary-schema.md). Stage 1: the generic core (data, actions, graph,
 * builders, engine, reporters). Language skins (java, cpp, ts, python) are
 * Stage 2, each lowering to these same core actions.
 */

export {
  DataExpr,
  DataAtom,
  FileData,
  GlobData,
  RegexData,
  ToolData,
  ValueData,
  DataUnion,
  DataFactory,
  data,
} from "./data";

export {
  Action,
  Expandable,
  ExternalProcessAction,
  ExpandAll,
} from "./actions";
export type {
  WorkerSpec,
  ExternalProcessOptions,
  ExpandAllOptions,
} from "./actions";

export { Graph, EdgeTrigger } from "./graph";

export {
  CleanBuilder,
  IncrementalBuilder,
  CacheBuilder,
  AtomicBuilder,
  buildStack,
} from "./builders";
export type {
  NestableBuilder,
  GraphSupplier,
  BuildOptions,
} from "./builders";

export { ConsoleReporter } from "./report";
export type { Reporter } from "./report";

export type {
  BuildResult,
  ProcessResult,
  NodeRef,
  NodeState,
} from "./results";
export { summarize } from "./results";

export { graphToSpec } from "./lowering";
export type { Spec } from "./lowering";

export { runStatus, runClean } from "./engine";
export type { StatusReport } from "./engine";

export * as cli from "./cli";
export { Alias } from "./cli";

export { SYSTEM, UNSET, PASS } from "./env";
export type { EnvSentinel } from "./env";

// Language skins (Stage 2) — each lowers to the core actions above.
export * as python from "./python";
export * as ts from "./ts";
export * as java from "./java";
export * as cpp from "./cpp";
export * as rust from "./rust";
export * as kotlin from "./kotlin";
