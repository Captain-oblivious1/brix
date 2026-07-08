//! The incremental layer's behavioral contract, mirrored case-for-case from
//! `tests/test_incremental.py` — either engine must satisfy these. Shell
//! scripts stand in for the Python helper tools; each script appends a line
//! to `runs.log` so the tests can count real process runs (a replayed node
//! leaves no line).
//!
//! Every build constructs a fresh `BuildMetadata` and loads it from disk —
//! the strongest cross-build property (each CLI invocation does exactly
//! that), and what pins the metadata.json interop.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use brix_core::executor::{execute, BuildResult};
use brix_core::incremental::{scan_depfile, IncrementalLayer};
use brix_core::metadata::BuildMetadata;
use brix_core::plan::{ExecutionPlan, NodeState};
use brix_core::report::NullReporter;

// -- helpers ------------------------------------------------------------------

fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-incremental-{name}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write(root: &Path, rel: &str, content: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, content).unwrap();
}

/// Logs `upper:<source>`, prints `uppercased <source>` (the replay-stdout
/// probe), and uppercases source -> dest. The Python `UPPER_TOOL`.
const UPPER_SH: &str = "echo \"upper:$1\" >> runs.log\n\
                        echo \"uppercased $1\"\n\
                        tr a-z A-Z < \"$1\" > \"$2\"\n";

/// Logs `combine` and concatenates every remaining arg into the first.
const CONCAT_SH: &str = "echo combine >> runs.log\n\
                         out=\"$1\"\n\
                         shift\n\
                         cat \"$@\" > \"$out\"\n";

fn write_workspace(root: &Path, sources: &[(&str, &str)]) {
    for (name, text) in sources {
        write(root, &format!("src/{name}.txt"), text);
    }
    write(root, "tools/upper.sh", UPPER_SH);
    write(root, "tools/concat.sh", CONCAT_SH);
}

/// The upper+combine graph from the Python suite, wire-shaped. The shared
/// `{shell}` tool atom and `{tool}` script file are fingerprint inputs of
/// every action, exactly like `data.tool(sys.executable)` / the tool scripts
/// in Python.
fn upper_combine_spec() -> String {
    r#"{
      "version": 1,
      "actions": [
        {
          "kind": "expand",
          "element": "name",
          "collection": [{"kind": "glob", "template": "src/{name}.txt"}],
          "child": {
            "kind": "process",
            "name": "upper {name}",
            "command_line": "{shell} {tool} {source} {dest}",
            "inputs": {
              "shell": [{"kind": "tool", "name": "sh"}],
              "tool": [{"kind": "file", "path": "tools/upper.sh"}],
              "source": [{"kind": "file", "path": "src/{name}.txt"}]
            },
            "outputs": {"dest": [{"kind": "file", "path": "build/upper/{name}.txt"}]}
          }
        },
        {
          "kind": "process",
          "name": "combine",
          "command_line": "{shell} {tool} {combined} {parts}",
          "inputs": {
            "shell": [{"kind": "tool", "name": "sh"}],
            "tool": [{"kind": "file", "path": "tools/concat.sh"}],
            "parts": [{"kind": "glob", "template": "build/upper/{name}.txt"}]
          },
          "outputs": {"combined": [{"kind": "file", "path": "build/all.txt"}]}
        }
      ]
    }"#
    .to_string()
}

/// One build: plan from the wire spec, fresh metadata loaded from disk, the
/// real `IncrementalLayer`, the real scheduler.
fn build(root: &Path, spec_json: &str) -> (BuildResult, ExecutionPlan) {
    let spec = brix_core::spec::parse(spec_json).unwrap();
    let mut plan =
        brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap();
    let metadata = Arc::new(BuildMetadata::new(root));
    metadata.load();
    let layer = IncrementalLayer::new(metadata);
    let result = execute(&mut plan, Some(2), &[&layer], &NullReporter);
    (result, plan)
}

/// `Counter(runs.log lines)` — how many times each tool really ran.
fn runs(root: &Path) -> HashMap<String, usize> {
    let mut counts = HashMap::new();
    if let Ok(text) = std::fs::read_to_string(root.join("runs.log")) {
        for line in text.lines() {
            *counts.entry(line.to_string()).or_insert(0) += 1;
        }
    }
    counts
}

fn count(counts: &HashMap<String, usize>, key: &str) -> usize {
    counts.get(key).copied().unwrap_or(0)
}

fn read(root: &Path, rel: &str) -> String {
    std::fs::read_to_string(root.join(rel)).unwrap()
}

// -- freshness and replay -------------------------------------------------------

#[test]
fn second_build_runs_nothing() {
    let root = scratch("second-fresh");
    write_workspace(&root, &[("a", "hi "), ("b", "there")]);

    let (first, _) = build(&root, &upper_combine_spec());
    assert!(first.success, "errors: {:?}", first.errors);
    assert_eq!(read(&root, "build/all.txt"), "HI THERE");
    let after_first = runs(&root);
    assert_eq!(count(&after_first, "upper:src/a.txt"), 1);
    assert_eq!(count(&after_first, "upper:src/b.txt"), 1);
    assert_eq!(count(&after_first, "combine"), 1);

    let (second, plan) = build(&root, &upper_combine_spec());
    assert!(second.success);
    assert_eq!(runs(&root), after_first); // no process ran
    for node in &plan.nodes {
        assert_eq!(second.states.status(node.id), NodeState::Done, "{}", node.label);
    }
}

#[test]
fn replayed_result_carries_previous_stdout() {
    let root = scratch("replay-stdout");
    write_workspace(&root, &[("a", "x")]);
    build(&root, &upper_combine_spec());
    let (second, _) = build(&root, &upper_combine_spec());
    let replayed: Vec<_> = second
        .process_results
        .iter()
        .filter(|r| r.stdout.contains("uppercased"))
        .collect();
    assert!(!replayed.is_empty(), "previous stdout should replay on skip");
    assert!(replayed.iter().all(|r| r.source == "replayed"));
}

#[test]
fn changed_source_reruns_only_affected() {
    let root = scratch("changed-source");
    write_workspace(&root, &[("a", "one"), ("b", "two")]);
    build(&root, &upper_combine_spec());

    write(&root, "src/a.txt", "one changed");
    let (result, _) = build(&root, &upper_combine_spec());
    assert!(result.success);
    let counts = runs(&root);
    assert_eq!(count(&counts, "upper:src/a.txt"), 2); // reran
    assert_eq!(count(&counts, "upper:src/b.txt"), 1); // skipped
    assert_eq!(count(&counts, "combine"), 2); // its input content changed
    assert!(read(&root, "build/all.txt").contains("ONE CHANGED"));
}

#[test]
fn deleted_output_rebuilds_but_downstream_stays_fresh() {
    let root = scratch("deleted-output");
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]);
    build(&root, &upper_combine_spec());

    std::fs::remove_file(root.join("build/upper/a.txt")).unwrap();
    let (result, _) = build(&root, &upper_combine_spec());
    assert!(result.success);
    let counts = runs(&root);
    assert_eq!(count(&counts, "upper:src/a.txt"), 2); // output was gone, must rerun
    assert_eq!(count(&counts, "upper:src/b.txt"), 1);
    // The rerun produced byte-identical output, so combine stayed fresh.
    assert_eq!(count(&counts, "combine"), 1);
    assert_eq!(read(&root, "build/upper/a.txt"), "AA");
}

#[test]
fn changed_value_input_reruns() {
    // Same command shape, different value: fingerprint changes, key does not.
    let root = scratch("value-change");
    write(
        &root,
        "tools/gen.sh",
        "echo gen >> runs.log\nout=\"$1\"\nshift\nprintf '%s' \"$@\" > \"$out\"\n",
    );
    let spec_with_flag = |flag: &str| {
        format!(
            r#"{{
              "version": 1,
              "actions": [{{
                "kind": "process",
                "name": "gen",
                "command_line": "{{shell}} {{tool}} {{out}} {{flag}}",
                "inputs": {{
                  "shell": [{{"kind": "tool", "name": "sh"}}],
                  "tool": [{{"kind": "file", "path": "tools/gen.sh"}}],
                  "flag": [{{"kind": "value", "text": "{flag}"}}]
                }},
                "outputs": {{"out": [{{"kind": "file", "path": "build/out.txt"}}]}}
              }}]
            }}"#
        )
    };

    assert!(build(&root, &spec_with_flag("-O2")).0.success);
    assert_eq!(read(&root, "build/out.txt"), "-O2");
    assert_eq!(count(&runs(&root), "gen"), 1);

    assert!(build(&root, &spec_with_flag("-O3")).0.success);
    assert_eq!(read(&root, "build/out.txt"), "-O3");
    assert_eq!(count(&runs(&root), "gen"), 2);

    assert!(build(&root, &spec_with_flag("-O3")).0.success);
    assert_eq!(count(&runs(&root), "gen"), 2); // fresh again
}

#[test]
fn command_line_change_refingerprints() {
    // The command template is fingerprint material: changing it re-stales the
    // action even when every declared input is byte-identical.
    let root = scratch("command-change");
    write(
        &root,
        "tools/gen.sh",
        "echo run >> runs.log\nout=\"$1\"\nshift\nprintf '%s' \"$@\" > \"$out\"\n",
    );
    let spec_with_label = |label: &str| {
        format!(
            r#"{{
              "version": 1,
              "actions": [{{
                "kind": "process",
                "name": "gen",
                "command_line": "{{shell}} {{tool}} {{out}} {label}",
                "inputs": {{
                  "shell": [{{"kind": "tool", "name": "sh"}}],
                  "tool": [{{"kind": "file", "path": "tools/gen.sh"}}]
                }},
                "outputs": {{"out": [{{"kind": "file", "path": "out.txt"}}]}}
              }}]
            }}"#
        )
    };

    assert!(build(&root, &spec_with_label("first")).0.success);
    assert_eq!(read(&root, "out.txt"), "first");
    assert!(build(&root, &spec_with_label("second")).0.success);
    assert_eq!(read(&root, "out.txt"), "second"); // command change reran it
    assert!(build(&root, &spec_with_label("second")).0.success);
    assert_eq!(count(&runs(&root), "run"), 2); // third build was fresh
}

// -- failure is never fresh -----------------------------------------------------

#[test]
fn failed_action_outputs_removed_and_not_replayed() {
    let root = scratch("failed-removed");
    write(
        &root,
        "tools/fail.sh",
        "echo fail >> runs.log\necho 'partial garbage' > \"$1\"\nexit 2\n",
    );
    let spec = r#"{
      "version": 1,
      "actions": [{
        "kind": "process",
        "name": "half-write then fail",
        "command_line": "{shell} {tool} {out}",
        "inputs": {
          "shell": [{"kind": "tool", "name": "sh"}],
          "tool": [{"kind": "file", "path": "tools/fail.sh"}]
        },
        "outputs": {"out": [{"kind": "file", "path": "build/out.txt"}]}
      }]
    }"#;

    let (first, _) = build(&root, spec);
    assert!(!first.success);
    // A clean build would not have produced this output; neither may we.
    assert!(!root.join("build/out.txt").exists());

    let (second, _) = build(&root, spec);
    assert!(!second.success);
    assert_eq!(count(&runs(&root), "fail"), 2); // failure was never treated as fresh
}

// -- vanished members -------------------------------------------------------------

#[test]
fn deleted_source_removes_stale_output_and_recombines() {
    let root = scratch("deleted-source");
    write_workspace(&root, &[("a", "aa"), ("b", "bb"), ("c", "cc")]);
    build(&root, &upper_combine_spec());
    assert_eq!(read(&root, "build/all.txt"), "AABBCC");

    std::fs::remove_file(root.join("src/c.txt")).unwrap();
    let (result, plan) = build(&root, &upper_combine_spec());
    assert!(result.success, "errors: {:?}", result.errors);
    // The stale output is gone and the combined output matches a clean build.
    assert!(!root.join("build/upper/c.txt").exists());
    assert_eq!(read(&root, "build/all.txt"), "AABB");
    // The deletion was a real, visible node in the plan.
    let rm_nodes: Vec<_> = plan.nodes.iter().filter(|n| n.label.starts_with("rm ")).collect();
    assert_eq!(rm_nodes.len(), 1);
    assert!(rm_nodes[0].label.contains("build/upper/c.txt"));
    // Surviving members stayed fresh; only the combine reran.
    let counts = runs(&root);
    assert_eq!(count(&counts, "upper:src/a.txt"), 1);
    assert_eq!(count(&counts, "upper:src/b.txt"), 1);
    assert_eq!(count(&counts, "combine"), 2);

    // Next build: membership history is settled, nothing to delete or run.
    let (third, plan) = build(&root, &upper_combine_spec());
    assert!(third.success);
    assert!(!plan.nodes.iter().any(|n| n.label.starts_with("rm ")));
    assert_eq!(runs(&root), counts);
}

#[test]
fn readded_source_rebuilds_without_deletion() {
    let root = scratch("readded-source");
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]);
    build(&root, &upper_combine_spec());
    std::fs::remove_file(root.join("src/b.txt")).unwrap();
    build(&root, &upper_combine_spec());
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]); // b comes back
    let (result, plan) = build(&root, &upper_combine_spec());
    assert!(result.success, "errors: {:?}", result.errors);
    assert!(!plan.nodes.iter().any(|n| n.label.starts_with("rm ")));
    assert_eq!(read(&root, "build/all.txt"), "AABB");
}

// -- persistence ------------------------------------------------------------------

#[test]
fn metadata_file_is_written() {
    let root = scratch("metadata-written");
    write_workspace(&root, &[("a", "x")]);
    build(&root, &upper_combine_spec());
    let text = read(&root, "build/.brix/metadata.json");
    let stored: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert!(
        !stored["actions"].as_object().unwrap().is_empty(),
        "action records should be persisted"
    );
}

// -- "never make a stale hit" invariants ------------------------------------------
//
// These pin the completeness of fingerprint invalidation: any change that
// could affect an output must re-stale its action (and only content, not
// timestamps, decides downstream freshness). See the Python originals for the
// full rationale — they are the production-safety property for a monorepo.

#[test]
fn added_source_propagates_to_glob_consumer() {
    // Growing a glob input's membership must re-stale its consumer even
    // though no existing file changed — the counterpart to deleted-member
    // cleanup. Keying the glob fingerprint only on existing members' hashes
    // (not the member set) would leave `combine` stale-fresh.
    let root = scratch("added-source");
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]);
    build(&root, &upper_combine_spec());
    assert_eq!(read(&root, "build/all.txt"), "AABB");

    write_workspace(&root, &[("a", "aa"), ("b", "bb"), ("c", "cc")]); // add member c
    let (result, _) = build(&root, &upper_combine_spec());
    assert!(result.success);
    assert_eq!(read(&root, "build/all.txt"), "AABBCC");
    let counts = runs(&root);
    assert_eq!(count(&counts, "upper:src/c.txt"), 1); // new member compiled
    assert_eq!(count(&counts, "upper:src/a.txt"), 1); // rewritten same-content -> stayed fresh
    assert_eq!(count(&counts, "upper:src/b.txt"), 1);
    assert_eq!(count(&counts, "combine"), 2); // glob consumer re-staled by the new member

    let (third, _) = build(&root, &upper_combine_spec()); // settled: nothing left to do
    assert!(third.success);
    assert_eq!(runs(&root), counts);
}

#[test]
fn tool_change_invalidates_everything_it_built() {
    // Changing a shared file input (the upper.sh script every `upper` action
    // depends on) re-stales every action that used it — the "compiler upgrade
    // invalidates everything it built" property. Yet because the rebuilt
    // outputs are byte-identical, the downstream consumer stays fresh:
    // freshness is content-based, not timestamp-based.
    let root = scratch("tool-change");
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]);
    build(&root, &upper_combine_spec());

    write(&root, "tools/upper.sh", &format!("{UPPER_SH}# retuned\n")); // same outputs
    let (result, _) = build(&root, &upper_combine_spec());
    assert!(result.success);
    let counts = runs(&root);
    assert_eq!(count(&counts, "upper:src/a.txt"), 2); // both re-staled by the tool change
    assert_eq!(count(&counts, "upper:src/b.txt"), 2);
    assert_eq!(count(&counts, "combine"), 1); // outputs unchanged -> downstream stayed fresh
}

// -- discovered (depfile) dependencies ---------------------------------------------

#[test]
fn scan_depfile_parses_make_format() {
    let root = scratch("depfile-parse");
    // Line continuation, a system (absolute) header that must be dropped, and
    // a backslash-escaped space in a path.
    write(
        &root,
        "main.o.d",
        "build/obj/main.o: src/main.cpp include/util.h \\\n\
         \x20 include/other.h /usr/include/stdio.h src/a\\ b.h\n",
    );
    assert_eq!(
        scan_depfile(&root.join("main.o.d")),
        vec!["src/main.cpp", "include/util.h", "include/other.h", "src/a b.h"]
    );
    assert_eq!(scan_depfile(&root.join("missing.d")), Vec::<String>::new()); // unreadable -> empty
}

#[test]
fn depfile_discovered_dep_change_reruns() {
    // The header-dependency flow end to end: the tool reports undeclared
    // inputs (src/main.c, src/extra.h) via a Makefile-format depfile. The
    // same build refingerprints dep-aware, so an unchanged second build is
    // fresh — no spurious extra rebuild — and editing a discovered dep
    // re-stales the action even though no *declared* input changed.
    let root = scratch("depfile-flow");
    write(&root, "src/main.c", "int main;\n");
    write(&root, "src/extra.h", "#define X 1\n");
    write(
        &root,
        "tools/cc.sh",
        "echo cc >> runs.log\n\
         cat src/main.c src/extra.h > \"$1\"\n\
         printf 'build/out.txt: src/main.c src/extra.h\\n' > build/out.d\n",
    );
    let spec = r#"{
      "version": 1,
      "actions": [{
        "kind": "process",
        "name": "cc main",
        "command_line": "{shell} {tool} {out}",
        "inputs": {
          "shell": [{"kind": "tool", "name": "sh"}],
          "tool": [{"kind": "file", "path": "tools/cc.sh"}]
        },
        "outputs": {"out": [{"kind": "file", "path": "build/out.txt"}]},
        "depfile": "build/out.d"
      }]
    }"#;

    assert!(build(&root, spec).0.success);
    assert_eq!(count(&runs(&root), "cc"), 1);
    let stored: serde_json::Value =
        serde_json::from_str(&read(&root, "build/.brix/metadata.json")).unwrap();
    let deps: Vec<_> = stored["deps"]
        .as_object()
        .unwrap()
        .values()
        .flat_map(|v| v.as_array().unwrap().iter())
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(deps, vec!["src/extra.h", "src/main.c"]); // sorted, recorded

    assert!(build(&root, spec).0.success);
    assert_eq!(count(&runs(&root), "cc"), 1); // dep-aware record, still fresh

    write(&root, "src/extra.h", "#define X 2\n"); // an undeclared-but-discovered input
    assert!(build(&root, spec).0.success);
    assert_eq!(count(&runs(&root), "cc"), 2); // re-staled by the header edit
    assert!(read(&root, "build/out.txt").contains("#define X 2"));

    assert!(build(&root, spec).0.success);
    assert_eq!(count(&runs(&root), "cc"), 2); // settled again
}
