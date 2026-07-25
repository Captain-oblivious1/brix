# Capability-sufficiency audit

Status: **draft (2026-07-12)** — a deliberate step back from feature-by-feature
work to answer two questions directly:

1. **Sufficiency** — can brix express *any* build people reasonably want?
2. **Uniformity** — are capabilities the *same across languages*, or is there a
   confusing per-language matrix ("atomic works for Java but not TS in the same
   workspace")?

The driving invariant this audit proposes to adopt:

> **Uniform capabilities across languages.** A build *feature* (atomic staging,
> caching, incrementality, sandboxing) either works for every language or is
> off for every language. Languages may differ in *coverage* (which ecosystems
> have a dependency resolver) and *optimization* (which have a smart lens or a
> warm worker), but never in whether a correctness feature applies. No feature
> matrix a user has to memorize.

The headline finding: **there is exactly one true capability mismatch today**
(atomic staging under tool-self-resolved intra-build artifacts). Everything else
is either uniform, a mechanical coverage gap, or an acceptable performance
gradient. So the matrix the concern imagines is *one fix away from not existing*,
not an accumulating problem.

## The axes

**Languages / lanes today:** Java (JVM), C/C++, TypeScript, and the generic
process action every lane lowers to.

**Build features (capabilities):** correct incremental rebuild; content-lens
"smart skip"; automatic dependency discovery; result cache; atomic staging; warm
workers/daemon; third-party dependency resolution; deleted-source cleanup;
sandboxing (planned); remote execution (planned).

**Dependency patterns (how a step obtains an input) — the axis that actually
matters for the mismatch:**

| # | pattern | example | atomic-safe? |
|---|---------|---------|--------------|
| 1 | checked-in source | `src/x.ts`, `main.c` | ✅ always |
| 2 | checked-in file the tool resolves itself | `#include "config.h"` | ✅ (real path) |
| 3 | external dep via an ecosystem resolver | Maven jar, pkg-config lib | ✅ (brix-supplied path) |
| 4 | intra-build artifact via a **brix-supplied** path | javac `-cp {built.jar}` | ✅ (overlay-resolved) |
| 5 | intra-build artifact via the **tool's own** resolution | `import "../lib/dist/x"`, `#include "gen.pb.h"` | ❌ **the gap** |

Pattern 5 is the whole story. It is not a language; it is "a step consumes
another step's freshly-built output through the tool's own path resolution."

## Uniformity matrix

Verdict labels: **UNIFORM** (engine-level, identical everywhere) · **GRADED**
(correct everywhere; optimization varies — acceptable) · **COVERAGE** (model
uniform; per-ecosystem instance not yet built — mechanical) · **MISMATCH** (the
feature genuinely behaves differently by language/pattern — the thing to kill).

| capability | Java | C/C++ | TypeScript | verdict |
|---|---|---|---|---|
| Correct incremental rebuild | ✅ | ✅ | ✅ | **UNIFORM** — fingerprint skip is engine-level |
| Content-lens smart skip | `jvm-abi` | byte only | `ts-dts` | **GRADED** — all correct; some have a semantic lens, others fall back to byte hashing (still correct, just less clever). Add a lens per language as it pays off. |
| Automatic dependency discovery | whole-module (n/a) | `-MMD` depfiles auto-discover `#include`s | **manual** `dependencies=` (import-graph reader planned) | **COVERAGE** *with a soundness edge* — see below |
| Result cache (local + shared) | ✅ | ✅ | ✅ | **UNIFORM** — content-addressed over any process action |
| Atomic staging | ✅ | ✅ *(until a generated header)* | ❌ *(cross-module import)* | **MISMATCH** — pattern 5. The one true gap. |
| Warm worker / daemon | ✅ javac worker + daemon | spawn per step | spawn per step | **GRADED** — Java is faster; others are correct. Any lane *could* get a worker (`tsc --watch`-style); it is speed, not capability. |
| Third-party dep resolution | `pom`/`maven`/`gradle` | `pkg_config` (conan/vcpkg planned) | npm planned | **COVERAGE** — the *model* (`DepSet`, drive-resolver-consume-output) is uniform; instances land per ecosystem on demand |
| Deleted-source cleanup | ✅ | ✅ | ✅ | **UNIFORM** — engine-level |
| Sandboxing | planned | planned | planned | **UNIFORM** (when built) — filesystem-view control is engine-level |
| Remote execution | planned | planned | planned | **UNIFORM** (when built) |

## The one true mismatch (and why it is one fix, not a matrix)

**Atomic staging breaks under dependency pattern 5.** The `AtomicLayer` writes
outputs to a staging tree and resolves brix's *own* input atoms through an
overlay — but a subprocess that resolves paths *itself* (`tsc` on an `import`, a
compiler on a generated `#include`, Go, protobuf) reads the real workspace, where
this build's staged outputs are not. Java is immune only because the JVM
toolchain takes explicit path arguments (`-cp`, `-d`) that brix fills with the
overlay-resolved path — pattern 4, not 5. The failure is language-agnostic; TS
just hits it first because cross-module `import` is its *normal* case, while C
hits it only with generated headers and Java essentially never.

Because the root cause is a single property of the staging mechanism, **one fix
removes the entire matrix**: make a subprocess's own reads see the merged
(workspace + staging) view. That is the parked design note *"staged-overlay
visibility for foreign resolvers"* ([roadmap](roadmap.md#staged-overlay-visibility-for-foreign-resolvers)), and it is
the **same primitive as the Sandboxing project** (impose a filesystem view on a
step) — build them once. Until then, two escape hatches already keep builds
*correct*: the plain incremental stack (outputs written in place), or compiling a
project whole-program in one step (no cross-step self-resolution, coarser
incrementality).

**Correctness note:** the mismatch is not merely "atomic is unavailable for TS."
On an incremental rebuild where a *prior committed* declaration sits at the real
path, the tool reads the **stale** one and can silently ship a wrong output. So
the interim requirement is a **loud, uniform guard** (below), not silence.

## The other gaps, classified honestly (so we don't mistake them for the matrix)

- **Content-lens gradient (GRADED).** Missing a lens never breaks a build; it
  just rebuilds more than strictly necessary. Extensible per language (a
  `cpp-abi`, a `go-export` lens) as the payoff justifies. Not a mismatch.
- **Warm-worker gradient (GRADED).** A performance asymmetry, not a capability
  one — every lane builds correctly; Java is merely faster between builds.
- **Resolver coverage (COVERAGE).** `DepSet` + "drive a resolver, consume its
  output" is one uniform model; npm / conan / vcpkg are instances to add when a
  real project needs them. Expected, not confusing.
- **Automatic vs. manual dependency discovery — the one to elevate.** C
  auto-discovers `#include`s via `-MMD` depfiles, so its cross-file correctness
  is *automatic*. TS today requires the author to declare cross-module deps by
  hand (`dependencies=`); a missed declaration is a silent stale build — the
  correctness rests on the user, Makefile-style. The planned TS import-graph
  reader (feeding the existing depfile channel) closes it, and **sandboxing would
  catch under-declaration for every lane at once**. This is the second reason the
  overlay/sandbox primitive is foundational, not polish.

## Sufficiency: build shapes brix can and cannot express

Independent of uniformity — can the API express arbitrary builds? Mostly yes; the
real gaps are a short list.

**Expressible today:** compile / archive / link; codegen → compile (a generate
step feeding a compile step — with the pattern-5 atomic caveat if the generated
file is tool-resolved); selective test execution (`java.JUnitTestAction`, and
generically); packaging (jar, Spring Boot fat-jar; tar/zip/`docker build` as
plain process actions, though without incrementality *inside* the opaque tool);
multi-stage bootstraps; and **dynamic graphs** — `ExpandAll` expands over
collections a generator action produces *during the same build*, so
"discovered-at-build-time" work is first-class.

**Sufficiency gaps found:**

1. **Pure-ordering edges (no shared file) — DONE.** `Graph.force_order(before,
   after)` expresses ordering that is not mediated by a file, with typed
   `EdgeTrigger` control over success/completion behavior. Target selection
   pulls in the forced predecessor, and the relationship crosses the shared
   Rust boundary.
2. **Build variants / configuration axis.** Debug/release, target arch, feature
   flags — buildable by hand by parameterizing the graph, but there is no
   first-class "variant" concept, so "build all N variants" has no ergonomic
   expression. **Open question** — validate need against the real repo before
   designing.
3. **Opaque-tool packaging (e.g. `docker build`).** Works as a process action,
   but brix sees the image as a black box — no incrementality of its internals.
   Likely acceptable (delegate to the tool); flag it so it is a *decision*, not a
   surprise.

Out of scope by design (not gaps): **downloading** third-party deps (brix drives
a resolver, it is not a package manager) and intra-module class-level incremental
compilation (deliberate cut, `docs/java-fast-builds.md`).

## Verdict and prioritized actions

brix's model — a generic process graph, a data-expression algebra, content
lenses, `DepSet`, and composable layers — **is architecturally sufficient and
language-neutral.** The feared per-language matrix reduces to **one** true
mismatch plus a set of mechanical coverage gaps and acceptable performance
gradients. Concretely:

- **P0 — DONE (2026-07-12).** The uniformity invariant is now specification design
  rule 13, and the interim guard shipped: an action marks
  `self_resolved_inputs` (today `ts.CompileAction` with cross-module
  `dependencies=`), `atomic.staging_hazards(graph)` collects them, and
  `AtomicBuilder.build()` refuses — loud, engine-agnostic, before any work —
  with one uniform "use the plain stack" message. General by construction: any
  future foreign-resolver lane that marks its inputs is covered for free.
  `tests/test_ts.py` carries the guard tests.
- **P1 — the staged-overlay / sandbox primitive.** The single fix that erases the
  matrix and equalizes under-declaration safety. Decide the model (full merged
  view vs. "stage finals, write intermediates in place" vs. targeted
  materialization) against a *real* generated-artifact case, not the toy example.
  Fable-reviewed; unify with sandboxing.
- **P1 — pure-ordering edges.** The main *expressiveness* gap; blocks
  service-then-test and some codegen shapes. Self-contained, Opus-sized.
- **P2 — TS import-graph auto-discovery.** Equalizes automatic dependency
  discovery; removes the manual-declaration soundness edge (also covered by
  sandbox once that lands).
- **P2 — resolver coverage (npm, conan/vcpkg)** as real projects demand.
- **Ongoing / on-demand — content lenses and warm workers per language.**
  Optimization, not capability; schedule by payoff.
- **Validate against the real monorepo:** the variant/configuration axis and
  opaque-tool packaging — confirm whether they are real requirements before
  investing.
