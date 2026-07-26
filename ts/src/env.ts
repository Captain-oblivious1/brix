/**
 * Environment sentinels for `ExternalProcessAction({ env })` — the TS mirror of
 * `src/brix/env.py`. An `env` value is a literal (set + fingerprint) or a
 * sentinel carrying a different intent. See docs/environment-recording.md.
 *
 * - SYSTEM: take from the ambient environment and *fingerprint* it (track, don't
 *   pin) — desugars to `envUsed`, safe on the warm-worker path.
 * - UNSET: force the variable *absent* from the process environment.
 * - PASS: let it *through* without fingerprinting (noisy plumbing: PATH, HOME).
 */

export const SYSTEM: unique symbol = Symbol("brix.SYSTEM");
export const UNSET: unique symbol = Symbol("brix.UNSET");
export const PASS: unique symbol = Symbol("brix.PASS");

export type EnvSentinel = typeof SYSTEM | typeof UNSET | typeof PASS;
