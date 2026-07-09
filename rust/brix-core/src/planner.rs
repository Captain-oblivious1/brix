//! Planning: lower a list of logical actions into an execution plan.
//! Dependency inference mirrors `src/brix/planner.py` — an edge from A to B
//! when any output atom of A can name the same path as any input atom of B,
//! and the edge reason reports the *first* overlapping pair (so input/output
//! order matters, which is why actions keep insertion order). One refinement,
//! also mirrored: when A and B are expansions iterating structurally-equal
//! collections under the same element name, that binding holds the same value
//! on both sides and is compared rigidly (`overlaps_shared`) — without it,
//! `combine {video}` and `finalize {video}` over one `videos` collection look
//! mutually dependent and the plan reports a false cycle.

use crate::actions::Action;
use crate::data::{DataAtom, DataExpr};
use crate::patterns::overlaps_shared;
use crate::plan::{ExecutionPlan, NodeKind};
use crate::targets::select_actions;

/// Decide whether a producer output atom and a consumer input atom can name
/// the same path. Mirrors `_atoms_overlap`: two regex atoms never compare;
/// a regex vs a concrete path uses full-match; otherwise pattern overlap
/// (with `shared` bindings compared rigidly).
pub(crate) fn atoms_overlap_shared(out: &DataAtom, inp: &DataAtom, shared: &[String]) -> bool {
    match (out, inp) {
        (DataAtom::Regex { .. }, DataAtom::Regex { .. }) => false,
        (DataAtom::Regex { .. }, _) => match inp.path_pattern() {
            Some(ip) => ip.is_concrete() && out.regex_match(&ip.template).is_some(),
            None => false,
        },
        (_, DataAtom::Regex { .. }) => match out.path_pattern() {
            Some(op) => op.is_concrete() && inp.regex_match(&op.template).is_some(),
            None => false,
        },
        _ => match (out.path_pattern(), inp.path_pattern()) {
            (Some(op), Some(ip)) => overlaps_shared(&op, &ip, shared),
            _ => false,
        },
    }
}

pub(crate) fn atoms_overlap(out: &DataAtom, inp: &DataAtom) -> bool {
    atoms_overlap_shared(out, inp, &[])
}

/// Element name -> the collection it iterates, for the expansion spine of
/// `action` (nested expansions contribute each level). Mirrors
/// `_expansion_bindings`; the Python side keys collections by atom `repr`,
/// which is structural — `DataExpr`'s derived equality is the same reading.
fn expansion_bindings(action: &Action) -> Vec<(&str, &DataExpr)> {
    let mut binds = Vec::new();
    let mut current = action;
    while let Action::Expand { element, collection, child } = current {
        binds.retain(|(name, _)| *name != element.as_str());
        binds.push((element.as_str(), collection));
        current = child;
    }
    binds
}

/// Binding names that hold the same value on both sides: element names of
/// expansions iterating structurally-equal collections. Mirrors
/// `shared_bindings`.
pub fn shared_bindings(producer: &Action, consumer: &Action) -> Vec<String> {
    let pa = expansion_bindings(producer);
    if pa.is_empty() {
        return Vec::new();
    }
    let pb = expansion_bindings(consumer);
    pa.iter()
        .filter(|(name, coll)| pb.iter().any(|(n, c)| n == name && c == coll))
        .map(|(name, _)| name.to_string())
        .collect()
}

/// An edge reason if `consumer` depends on `producer`, else None.
pub fn infer_dependency(producer: &Action, consumer: &Action) -> Option<String> {
    let shared = shared_bindings(producer, consumer);
    for out_atom in producer.output_atoms() {
        for in_atom in consumer.input_atoms() {
            if atoms_overlap_shared(&out_atom, &in_atom, &shared) {
                let out_text = out_atom.text();
                let in_text = in_atom.text();
                if out_text == in_text {
                    return Some(format!("data overlap: {out_text}"));
                }
                return Some(format!("data overlap: {out_text} ~ {in_text}"));
            }
        }
    }
    None
}

/// Lower actions (optionally narrowed to `targets`) into a plan with inferred
/// dependency edges. `root` is stored but does not affect the structure.
pub fn plan_graph(
    actions: Vec<Action>,
    root: &str,
    targets: Option<&[String]>,
) -> Result<ExecutionPlan, String> {
    let actions = match targets {
        Some(t) if !t.is_empty() => select_actions(&actions, t)?,
        _ => actions,
    };
    let mut plan = ExecutionPlan::new(root);
    for action in &actions {
        let kind = if matches!(action, Action::Expand { .. }) {
            NodeKind::Expand
        } else {
            NodeKind::Process
        };
        plan.add_node(kind, action.label(), action.clone());
    }
    for pi in 0..actions.len() {
        for ci in 0..actions.len() {
            if pi == ci {
                continue;
            }
            if let Some(reason) = infer_dependency(&actions[pi], &actions[ci]) {
                plan.add_edge(pi, ci, reason);
            }
        }
    }
    Ok(plan)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn combine_finalize(finalize_collection: &str) -> Vec<Action> {
        let spec = format!(
            r#"[
            {{"kind": "expand", "element": "video",
              "collection": [{{"kind": "glob", "template": "src/{{video}}.py"}}],
              "child": {{"kind": "process", "name": "combine {{video}}",
                         "command_line": "combine {{scenes}} {{raw}}",
                         "inputs": {{"scenes": [{{"kind": "glob", "template": "out/{{video}}/{{scene}}.mpg"}}]}},
                         "outputs": {{"raw": [{{"kind": "file", "path": "out/{{video}}.raw.mpg"}}]}}}}}},
            {{"kind": "expand", "element": "video",
              "collection": [{{"kind": "glob", "template": "{finalize_collection}"}}],
              "child": {{"kind": "process", "name": "finalize {{video}}",
                         "command_line": "finalize {{raw}} {{mpg}}",
                         "inputs": {{"raw": [{{"kind": "file", "path": "out/{{video}}.raw.mpg"}}]}},
                         "outputs": {{"mpg": [{{"kind": "file", "path": "out/{{video}}.mpg"}}]}}}}}}
            ]"#
        );
        serde_json::from_str(&spec).unwrap()
    }

    // Mirrors test_same_collection_expansions_do_not_false_cycle in
    // tests/test_plan.py: both expansions iterate one `videos` collection, so
    // their {video}s hold the same value — combine -> finalize (the .raw.mpg
    // handoff) is kept, and the recursive-split reverse edge is not invented.
    #[test]
    fn same_collection_expansions_do_not_false_cycle() {
        let plan = plan_graph(combine_finalize("src/{video}.py"), "/root", None).unwrap();
        assert!(plan.edges.iter().any(|e| e.from_id == 0 && e.to_id == 1));
        assert!(!plan.edges.iter().any(|e| e.from_id == 1 && e.to_id == 0));
    }

    // Mirrors test_different_collections_keep_the_conservative_edge: with
    // *different* collections the {video}s are independent values, so the
    // conservative overlap (and the cycle it implies) must stay.
    #[test]
    fn different_collections_keep_the_conservative_edge() {
        let plan = plan_graph(combine_finalize("other/{video}.toml"), "/root", None).unwrap();
        assert!(plan.edges.iter().any(|e| e.from_id == 1 && e.to_id == 0));
    }
}
