//! Planning: lower a list of logical actions into an execution plan.
//! Dependency inference mirrors `src/brix/planner.py` — an edge from A to B
//! when any output atom of A can name the same path as any input atom of B,
//! and the edge reason reports the *first* overlapping pair (so input/output
//! order matters, which is why actions keep insertion order).

use crate::actions::Action;
use crate::data::DataAtom;
use crate::patterns::overlaps;
use crate::plan::{ExecutionPlan, NodeKind};
use crate::targets::select_actions;

/// Decide whether a producer output atom and a consumer input atom can name
/// the same path. Mirrors `_atoms_overlap`: two regex atoms never compare;
/// a regex vs a concrete path uses full-match; otherwise pattern overlap.
pub(crate) fn atoms_overlap(out: &DataAtom, inp: &DataAtom) -> bool {
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
            (Some(op), Some(ip)) => overlaps(&op, &ip),
            _ => false,
        },
    }
}

/// An edge reason if `consumer` depends on `producer`, else None.
pub fn infer_dependency(producer: &Action, consumer: &Action) -> Option<String> {
    for out_atom in producer.output_atoms() {
        for in_atom in consumer.input_atoms() {
            if atoms_overlap(&out_atom, &in_atom) {
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
