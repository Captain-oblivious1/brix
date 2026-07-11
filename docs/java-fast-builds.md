# Fast JVM builds — design

Goal: Java builds as fast as or faster than Gradle, without inheriting
Gradle's complexity. This doc is the execution contract for the two
engine concepts that requires — **persistent workers** and
**content-identity fingerprints (ABI hashing)** — plus the `brix.java`
changes that ride on them. Like `docs/rust-port.md`, it is written so a
model that didn't design brix can implement it without reinterpreting
anything. The ground rules in README.md still bind; nothing here bends
them.

## Why Gradle is fast, and what brix already has

A JVM spawn costs ~1s before javac compiles anything, and a *cold* JVM
compiles several times slower than a warm one. Gradle attacks this three
ways: a daemon (warm JVM), compile avoidance (don't recompile dependents
when an implementation changed but the API didn't), and class-level
incremental compilation within a module.

Brix's position today:

- `JarAction` already batches: **one** javac invocation per jar, never
  per file. Module-level granularity is the design center.
- No-op rebuilds replay with **zero** JVM launches — already faster than
  a daemon answering "nothing to do".
- Clean builds restore from the cache layer — no JVM either.

The gap is the *edit-compile loop*: change one file, rebuild. Today that
costs one cold JVM per stale module, and an implementation-only change
in a low-level module recompiles every dependent. Concept 1 fixes the
cold JVM; concept 2 fixes the over-recompilation.

**Deliberately out of scope:** class-level incremental compilation
within a module (Gradle's third trick). Enormous complexity, modest
payoff when modules are reasonably sized, and it fights brix's
action-level model. Keep modules small instead.

---

## Concept 1: Persistent workers

A worker is a long-lived helper process that executes many requests, so
its startup/warmup cost amortizes. It is purely an **execution
transport**: an action executed by a worker must be indistinguishable —
in results, fingerprints, caching, staging, and reporting — from the
same action spawned as a process. If a behavior difference is ever
observable outside timing, the implementation is wrong.

### API surface

```python
javac_worker = WorkerSpec(
    name="javac",
    command_line="{java} -jar {worker_jar}",   # rendered like any action
    inputs={"java": data.tool("java"), "worker_jar": data.file("...")},
)

ExternalProcessAction(
    name="compile {module}",
    worker=javac_worker,                # NEW: route via worker
    command_line="--sources {sources} --classpath {classpath} -d {classes_dir}",
    inputs={...}, outputs={...},
)
```

With `worker=` set, `command_line` renders exactly as today (overlay
redirection of output paths included — this is what keeps atomic
staging working) but instead of spawning, the rendered argv is sent to
the worker as a request.

### Protocol (language-neutral by design; the JS/TS API inherits it)

JSON lines over the worker's stdin/stdout. Engine → worker:

```json
{"id": 7, "args": ["--sources", "src/A.java", "..."], "cwd": "/abs/workspace"}
```

Worker → engine:

```json
{"id": 7, "exitCode": 0, "stdout": "...", "stderr": "..."}
```

Rules:

- One request in flight per worker process (v1). Parallelism = the
  scheduler starting up to `jobs` worker processes of the same spec.
- The worker must be **stateless between requests**: no caching keyed on
  anything but the request args, no cwd-relative state retained. `cwd`
  travels per-request so a future cross-build daemon can serve multiple
  workspaces.
- The worker's own stdout is reserved for the protocol; tool output goes
  in the response fields.

### Semantics (do not reinterpret)

- **ProcessResult mapping**: `command` = the rendered request args
  joined; `source="process"` (it ran); `process_started` fires when the
  request is dispatched. Reporters cannot tell the difference; nothing
  new crosses the reporter seam.
- **Fingerprints**: the action fingerprint gains the worker's identity —
  a `"worker"` section built from the WorkerSpec's rendered command and
  input atoms, fingerprinted with the same `atom_fp` rules as action
  inputs (tool = resolved path + stat; files by content hash). Same
  javac + same wrapper ⇒ same key; upgrade the JDK ⇒ everything
  worker-built goes stale, exactly like a compiler upgrade today.
- **Worker crash / protocol garbage**: the in-flight request fails with
  `exit_code=None` and a `brix:` stderr — like a signal kill, it is not
  the action's deterministic answer, so it is **never cached** and never
  recorded as a cacheable failure. The worker process is not reused
  afterward; subsequent requests start a fresh one.
- **Worker startup failure**: every request routed to it fails the same
  way (`exit_code=None`). No silent fallback to spawning — a
  half-configured worker must be loud, not slow.
- Scheduler owns worker lifecycles: started lazily on first request,
  all terminated at build end (v1). Workers are engine plumbing —
  layers never see them; `around_node` wraps request dispatch exactly
  as it wraps spawning today.

### v2: cross-build daemon (design settled 2026-07; Rust engine only)

The edit-compile loop wants the JVM warm *between* builds. v2 keeps
worker processes alive after build end, keyed by their worker
fingerprint. The build process itself exits, so persistence lives in a
**holder daemon** — `brix-workerd`, a small Rust binary built alongside
the `brix_py` extension (same discovery rules):

- Instead of spawning the worker directly, the engine spawns
  `brix-workerd`, which detaches (double-fork/setsid), spawns the real
  worker child on stdin/stdout pipes, and listens on a Unix socket,
  bridging **one client at a time** to the worker's pipes. The JSON-line
  protocol and the workers themselves are byte-for-byte unchanged —
  the holder is transport plumbing only.
- Registry: `<metadata_dir>/workers/<worker-fingerprint>/<n>.sock` (+
  pidfile). The key is the same worker-section fingerprint that joins
  action fingerprints (rendered launch command + input atom fps), so a
  JDK upgrade or a rebuilt worker jar changes the key: the next build
  finds no daemon and starts fresh; the orphan idles out by itself —
  fingerprint-keyed replacement with no kill logic.
- Engine lifecycle: `check_out` adopts an idle daemon socket for the
  fingerprint when one answers (a stale/busy/dead socket is skipped —
  connect failure deletes the entry and a fresh daemon starts); build
  end *releases* workers back to their holders instead of terminating.
  Crash semantics are v1's, unchanged: a crashed or protocol-garbled
  worker is discarded, never reused, and its registry entry removed.
- **Idle timeout, one knob**: `worker_idle_timeout` seconds, default
  900. The holder enforces it (a build that never comes back cannot);
  on expiry it kills the worker, removes its entry, and exits.
  **0 disables persistence entirely** — the engine skips the holder and
  uses v1 direct pipes with terminate-at-build-end; the CLI spells that
  `--no-daemon`. The timeout rides the boundary *config* (never
  `WorkerSpec` and never fingerprint material: changing it must not
  rebuild anything).
- Concurrent builds: one request in flight per worker still holds; a
  busy socket just means the pool starts another daemon — parallelism
  by process count, exactly like v1.
- Rust engine only. Under `BRIX_ENGINE=py` workers keep v1
  die-at-build-end semantics until the Python engine internals are
  deleted (rust-port.md phase 4c); daemon tests gate on the extension.

v1's load-bearing preparations (per-request `cwd`, the statelessness
contract, fingerprint keying) are exactly what makes this a transport
change and not a semantics change.

---

## Concept 2: Content-identity fingerprints (ABI hashing)

Compile avoidance: module A depends on `util.jar`; when util's
*implementation* changes but its API doesn't, A must not recompile. In
brix terms: A's fingerprint should contain util.jar's **ABI hash**, not
its byte hash.

### API surface

```python
"classpath": java.abi(data.file("build/lib/util.jar")),
```

The author never types the lens name — `java.abi(expr)` views an expression
through the JVM ABI lens, rewriting each file/glob atom to its typed
counterpart (`brix.java.AbiFile`/`AbiGlob`), which carries
`content_key = "jvm-abi"`. The core dispatches that name through its
`ContentKey` registry (`docs/content-key.md`); the reference computation is a
registered lens:

```python
class ContentIdentity:
    name: str        # "jvm-abi"
    version: str     # "1" — bump on ANY change to extraction/normalization
    def digest(self, path: Path) -> str: ...
```

Engine changes:

- `atom_fp` for an atom with a `content_key` becomes
  `["file", path, "jvm-abi@1", digest]` — the **versioned tag is part of
  the fingerprint payload**, so a lens change invalidates cleanly.
- Extraction failure (not a jar, malformed class) falls back to the
  content hash under a distinct tag: `["file", path, "jvm-abi@1:raw",
  content_hash]`. Deterministic for the same bytes; a cross-machine
  disagreement can only cause a cache **miss**, never a stale hit.
- The per-file digest cache in metadata grows one entry per identity:
  `{"sig": [...], "sha256": ..., "jvm-abi@1": ...}` — same
  stat-signature validation, so unchanged jars cost one stat.
- Identity applies to *fingerprint material only*. `outputs_intact`,
  cache storage, staging, and overlay reads keep using bytes — identity
  never changes what is stored or copied, only when dependents rebuild.

### The correctness rule (do not reinterpret)

The ABI hash must **over-approximate**: anything that can affect the
compilation of a dependent must be included. For `jvm-abi@1`, from each
class file (classes in a jar processed in sorted-name order, digests
concatenated):

- class name, modifiers, superclass, interfaces, generic signature;
- every non-private field and method: name, descriptor, generic
  signature, modifiers, `throws` clauses;
- **`static final` constant values** (javac inlines compile-time
  constants into dependents — omitting values is the classic unsound
  "ABI hash");
- **all annotations, including class- and source-invisible-at-runtime
  retention, on the class and all non-private members** (annotation
  processors and compiler plugins read them);
- nest members / inner-class attributes affecting accessibility.

Excluded: method bodies, private members, debug info, line numbers.
Normalization: sort members canonically (name + descriptor); the digest
must not depend on constant-pool order or original member order.

Sharp edge to document for users: if an annotation *processor* consumes
non-ABI information (reads resource files, scans method bodies), ABI
identity on that dependency is unsound — don't opt in for it. Identity
is always opt-in per data expression; default remains byte hashing.

Implementation: a pure-Python class-file parser (the format is stable
and simple; parse constant pool + members + attributes — no JVM
required). Golden tests: checked-in `.class`/`.jar` fixtures with
expected digests, plus pairs proving implementation-only changes hash
equal and each ABI-affecting change (add method, change constant value,
change annotation) hashes different.

### Rust port impact (`docs/rust-port.md`)

- `ContentIdentity` joins `ChangeDetector` / `CacheStore` /
  `DependencyScanner` as the fourth narrow Python-pluggable trait;
  `jvm-abi` gets a native Rust implementation (classfile parsing is a
  solved problem in Rust) that must reproduce the Python digests —
  golden fixtures are the contract, same as fingerprint digests.
- The worker protocol and WorkerSpec fingerprint section land in the
  boundary contract; workers are scheduler-internal, so nothing new
  crosses to Python beyond the WorkerSpec data.

---

## brix.java on top

- `JarAction` grows `classpath=` (list of jar data expressions; rendered
  onto javac's `-cp`, viewing each entry through `java.abi(...)` — the
  JVM ABI lens), and lowers its compile step through the javac WorkerSpec.
- The javac worker is a small wrapper on `javax.tools.JavaCompiler`
  (in-process compilation, no re-exec), reading the JSON protocol.
  Bootstrap: ship the wrapper *source* with brix and compile it once via
  a plain spawned `javac` node in the same graph — the worker jar is
  itself a fingerprinted, cacheable build product.
- Multi-module builds are just multiple JarActions wired by
  `classpath=`; dependency inference connects them from the data-set
  intersections as usual.

## Acceptance benchmark (against Gradle 8 on the same modules)

Scenarios, each measured wall-clock: (1) clean build, cold cache;
(2) clean build, warm brix cache vs Gradle build cache; (3) no-op;
(4) implementation-only change in the lowest module; (5) API change in
the lowest module. Expected: brix wins 2 and 3 outright (no JVM at
all), ties 1 (both bounded by javac), and wins 4 with ABI identity +
worker (one module, one warm-ish JVM vs Gradle's daemon). Scenario 4/5
with the v2 daemon is the "faster than Gradle" claim; without it, parity.

## Phasing (each lands green on the full pytest suite)

1. **Content-identity fingerprints**: `content_key` plumbing (authored as
   `java.abi(...)`), versioned tags, per-lens metadata cache, `jvm-abi@1`
   parser + golden fixtures. No worker needed; immediately useful.
2. **Per-build workers**: WorkerSpec, protocol, pool, crash semantics,
   fingerprint section; javac wrapper + `JarAction` routing; the
   benchmark harness with scenarios 1–5.
3. **Cross-build daemon** (v2): registry, idle timeout,
   fingerprint-keyed replacement; rerun the benchmark.

Phases 1 and 2 are independent and can be built by a cheaper model
against this doc; the ABI extraction rules and the worker crash/caching
semantics above are the do-not-reinterpret core. Phase 3 touches
cross-build state and deserves review at full strength.
