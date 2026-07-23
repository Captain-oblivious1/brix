# Machine-readable status (`status --json`)

`brix_build.py status --json` emits the build state as a single JSON document on
stdout — the same information `status` prints as text, in a stable, versioned
shape a program can consume. It lets a web UI, IDE panel, or CI report render
build state without scraping human-readable terminal output. Brix provides the
data; it does not choose or host the frontend.

Like plain `status`, it inspects only — nothing builds, and the exit code is the
same: **0** when everything is fresh, **1** when work is pending (so it composes
into scripts the way the text form does). Errors (a bad target, an unreadable
build file) still print to *stderr* and return 2; stdout stays pure JSON on the
success path.

## Schema and versioning

The top-level `schema` field is the contract tag, currently **`brix.status/v1`**
(the constant `brix.status.STATUS_SCHEMA_VERSION`). It is deliberately
independent of the internal engine↔Python wire boundary
(`docs/boundary-schema.md`), which may change freely — this document is the
external promise.

Compatibility rule: **new fields may be added under the same version**, so a
consumer must ignore keys it does not recognize. The version is bumped only on a
*breaking* change (a removed or repurposed field). Both engines
(`BRIX_ENGINE=py` and the default Rust engine) produce an identical document.

## Fields

| Path | Type | Meaning |
|------|------|---------|
| `schema` | string | Schema version tag (`brix.status/v1`). |
| `clean` | bool | True when nothing is pending: every action fresh, no pending deletions, no staged/interrupted state. Matches the exit code (`clean` ⇒ exit 0). |
| `counts` | object | `state → count` over all actions (e.g. `{"fresh": 3}`). Keys are the same state strings used per action. |
| `actions` | array | One entry per planned action, in plan order. |
| `actions[].label` | string | Human-readable action label. |
| `actions[].state` | string | One of: `fresh`, `never built`, `inputs changed`, `outputs missing or modified`, `failed last build`, `outdated`. |
| `actions[].fresh` | bool | Convenience for `state == "fresh"`. |
| `actions[].detail` | string | Extra context (e.g. `"upstream will rebuild"` for an outdated action, or a failure summary); `""` when none. |
| `actions[].last_result` | object \| null | The last recorded run, or `null` if never built. See below. |
| `pending_deletions` | array | Outputs queued for deletion because their source expansion member vanished. |
| `pending_deletions[].expansion` | string | Label of the expansion the member left. |
| `pending_deletions[].member` | string | The vanished member. |
| `pending_deletions[].paths` | array of string | The concrete output paths that will be removed. |
| `staging` | object | Atomic staging state (all-empty/false when no atomic layer or no staging). |
| `staging.held` | bool | A successful build is staged, awaiting `commit`. |
| `staging.interrupted_commit` | bool | A commit was interrupted; the next build completes it. |
| `staging.staged_files` | array of string | Repo-relative paths held in staging. |
| `staging.staged_deletions` | array of string | Repo-relative paths staged for deletion. |
| `environment` | object | Present only from the CLI (`status --json`); the last-build environment diff. |
| `environment.changed` | bool | The current environment differs from the last recorded build. |
| `environment.note` | string \| null | The one-line human summary of the difference, or `null` when unchanged / nothing recorded. |

`state` is a closed set of strings; treat an unrecognized value as "not fresh"
rather than failing. The `environment` block is *diagnostic* — it never affects
the `clean` flag or the exit code, exactly as in the text form.

### `last_result`

When an action has a recorded run, `last_result` mirrors the stored process
result plus two derived conveniences:

| Field | Type | Meaning |
|-------|------|---------|
| `action_name` | string | The action's name. |
| `command` | string | The command template that ran. |
| `argv` | array of string | The concrete argument vector. |
| `exit_code` | int \| null | Process exit code (`null` if it never ran). |
| `signal` | int \| null | Terminating signal, if any. |
| `stdout` / `stderr` | string | Captured output. |
| `source` | string | Where the result came from (e.g. cache vs. fresh run). |
| `ok` | bool | Derived: `exit_code == 0`. |
| `summary` | string | Derived one-line summary (`"ok: <name>"` or `"FAILED (exit 4): <name>"`). |

## Example

A workspace built once, then one source edited — one action's inputs changed,
one is still fresh, and a downstream action is outdated because its upstream will
rebuild (`last_result` trimmed here for brevity):

```json
{
  "schema": "brix.status/v1",
  "clean": false,
  "counts": {
    "inputs changed": 1,
    "fresh": 1,
    "outdated": 1
  },
  "actions": [
    {
      "label": "upper a",
      "state": "inputs changed",
      "fresh": false,
      "detail": "",
      "last_result": { "action_name": "upper a", "exit_code": 0, "ok": true, "summary": "ok: upper a" }
    },
    {
      "label": "upper b",
      "state": "fresh",
      "fresh": true,
      "detail": "",
      "last_result": { "action_name": "upper b", "exit_code": 0, "ok": true, "summary": "ok: upper b" }
    },
    {
      "label": "combine",
      "state": "outdated",
      "fresh": false,
      "detail": "upstream will rebuild",
      "last_result": { "action_name": "combine", "exit_code": 0, "ok": true, "summary": "ok: combine" }
    }
  ],
  "pending_deletions": [],
  "staging": {
    "held": false,
    "interrupted_commit": false,
    "staged_files": [],
    "staged_deletions": []
  },
  "environment": {
    "changed": false,
    "note": null
  }
}
```

## Programmatic use (without the CLI)

`compute_status(...)` returns a `StatusReport`; its `to_public_dict()` method
produces the same document (minus the `environment` block unless you pass one):

```python
from brix import compute_status
import json

report = compute_status(create_graph, root=".")
print(json.dumps(report.to_public_dict(), indent=2))
```
