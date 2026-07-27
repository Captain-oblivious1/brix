/**
 * Lower a logical Graph to the language-neutral wire spec — the TS mirror of
 * `src/brix/lowering.py`.
 *
 * The spec format is pinned by docs/boundary-schema.md: the one surface the
 * Rust core, the Python skin, and this TS skin all build against. Wire keys are
 * snake_case (camelCase exists only in the authoring surface, mapped here at the
 * edge). Optional keys are emitted only when present, exactly as the Python
 * lowering does, so the produced spec deep-equals Python's for the same graph.
 */

import {
  Action,
  ExpandAll,
  ExternalProcessAction,
} from "./actions";
import {
  AssetData,
  DataAtom,
  DataExpr,
  FileData,
  GlobData,
  RegexData,
  ToolData,
  ValueData,
} from "./data";
import { EdgeTrigger, Graph } from "./graph";

export const SPEC_VERSION = 1;

export type AtomSpec = Record<string, unknown>;
export type Spec = Record<string, unknown>;

export function atomToSpec(atom: DataAtom): AtomSpec {
  if (atom instanceof FileData) {
    const d: AtomSpec = { kind: "file", path: atom.path };
    if (atom.contentKey !== undefined) d.content_key = atom.contentKey;
    return d;
  }
  if (atom instanceof GlobData) {
    const d: AtomSpec = { kind: "glob", template: atom.template };
    if (atom.contentKey !== undefined) d.content_key = atom.contentKey;
    if (atom.exclude.length) d.exclude = [...atom.exclude];
    return d;
  }
  if (atom instanceof RegexData) {
    return { kind: "regex", pattern: atom.pattern };
  }
  if (atom instanceof ToolData) {
    return { kind: "tool", name: atom.name };
  }
  if (atom instanceof ValueData) {
    return { kind: "value", text: atom.text };
  }
  if (atom instanceof AssetData) {
    return { kind: "asset", name: atom.name };
  }
  throw new TypeError(`unknown atom: ${atom}`);
}

export function exprToSpec(expr: DataExpr): AtomSpec[] {
  return expr.atoms().map(atomToSpec);
}

function exprMap(group: Record<string, DataExpr>): Record<string, AtomSpec[]> {
  const out: Record<string, AtomSpec[]> = {};
  for (const [k, v] of Object.entries(group)) out[k] = exprToSpec(v);
  return out;
}

export function actionToSpec(action: Action): Spec {
  if (action instanceof ExternalProcessAction) {
    const d: Spec = {
      kind: "process",
      command_line: action.commandLine,
      inputs: exprMap(action.inputs),
      outputs: exprMap(action.outputs),
    };
    if (action.name !== undefined) d.name = action.name;
    if (Object.keys(action.env).length) d.env = exprMap(action.env);
    if (action.envUnset.length) d.env_unset = [...action.envUnset];
    if (action.envPass.length) d.env_pass = [...action.envPass];
    if (action.envUsed.length) d.env_used = [...action.envUsed];
    if (action.depfile !== undefined) d.depfile = action.depfile;
    if (action.worker !== undefined) {
      d.worker = {
        name: action.worker.name,
        command_line: action.worker.commandLine,
        inputs: exprMap(action.worker.inputs),
      };
    }
    return d;
  }
  if (action instanceof ExpandAll) {
    return {
      kind: "expand",
      element: action.elementName,
      collection: exprToSpec(action.collection),
      child: actionToSpec(action.child),
    };
  }
  throw new TypeError(`unknown action: ${action}`);
}

export function graphToSpec(graph: Graph): Spec {
  const spec: Spec = {
    version: SPEC_VERSION,
    actions: graph.actions.map(actionToSpec),
  };
  if (graph.forcedOrders.length) {
    spec.forced_orders = graph.forcedOrders.map(
      ([before, after, trigger]: [number, number, EdgeTrigger]) => ({
        before,
        after,
        trigger,
      }),
    );
  }
  return spec;
}
