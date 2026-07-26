/**
 * The logical graph — the TS mirror of `src/brix/graph.py`.
 *
 * Holds action declarations. The engine infers dependencies from data-set
 * intersections; `forceOrder` covers the exceptional case where actions must
 * be ordered without sharing data.
 */

import { Action } from "./actions";
import { DataFactory } from "./data";

/** How a forced order is satisfied. Wire values per docs/boundary-schema.md. */
export enum EdgeTrigger {
  ON_SUCCESS = "on-success",
  ON_COMPLETION = "on-completion",
}

/** A skin action lowers into one or more core actions via `lower()`. */
interface Lowerable {
  lower(): Action[];
}

function isLowerable(action: unknown): action is Lowerable {
  return typeof (action as { lower?: unknown }).lower === "function";
}

export class Graph {
  readonly data = new DataFactory();
  readonly actions: Action[] = [];
  readonly forcedOrders: [number, number, EdgeTrigger][] = [];

  add(...actions: Action[]): this {
    // Depth-first, preserving order, mirroring Python's stack walk.
    const stack = [...actions].reverse();
    while (stack.length) {
      const action = stack.pop()!;
      if (!(action instanceof Action)) {
        throw new TypeError(`Graph.add expects Action instances, got ${action}`);
      }
      if (isLowerable(action)) {
        // Typed helpers (brix.cpp, …) lower into the generic model here.
        stack.push(...[...action.lower()].reverse());
      } else {
        this.actions.push(action);
      }
    }
    return this;
  }

  /**
   * Require `before` to finish before `after` may run — for ordering with no
   * data relationship. Both actions must already be added exactly once.
   */
  forceOrder(
    before: Action,
    after: Action,
    trigger: EdgeTrigger = EdgeTrigger.ON_SUCCESS,
  ): this {
    const positions: number[] = [];
    for (const action of [before, after]) {
      const found = this.actions
        .map((a, i) => (a === action ? i : -1))
        .filter((i) => i >= 0);
      if (found.length !== 1) {
        throw new Error(
          "forceOrder actions must each be added to this graph exactly once",
        );
      }
      positions.push(found[0]);
    }
    if (positions[0] === positions[1]) {
      throw new Error("forceOrder requires two different actions");
    }
    const [b, a] = positions;
    const existing = this.forcedOrders.find((o) => o[0] === b && o[1] === a);
    if (existing && existing[2] !== trigger) {
      throw new Error("forceOrder already exists with a different trigger");
    }
    if (!existing) this.forcedOrders.push([b, a, trigger]);
    return this;
  }
}
