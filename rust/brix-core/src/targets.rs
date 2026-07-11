//! Target selection: the subset (possibly binding-constrained) of actions
//! needed to produce the requested outputs. Demand-driven, on the logical
//! graph, before planning. Mirrors `src/brix/targets.py` — including the
//! shortest-stem rule (make's) that keeps suffix pipelines from diverging.

use std::collections::{BTreeMap, HashSet, VecDeque};

use crate::actions::Action;
use crate::data::DataAtom;
use crate::patterns::overlaps;
use crate::planner::atoms_overlap;

const PROBE: &str = "\u{0}target\u{0}";

/// Lower is more specific: total bound text (make's stem length). An
/// unprovable relation (no bindings) is least specific of all.
fn specificity(bindings: &[(String, String)]) -> usize {
    if bindings.is_empty() {
        usize::MAX
    } else {
        bindings.iter().map(|(_, v)| v.chars().count()).sum()
    }
}

/// How does producing `out_atom` help `need_atom`? `None` = unrelated;
/// `Some([])` = related but no constraint provable; otherwise the bindings
/// that specialize the producer to this need. Mirrors `_extract_bindings`.
fn extract_bindings(out_atom: &DataAtom, need_atom: &DataAtom) -> Option<Vec<(String, String)>> {
    if matches!(out_atom, DataAtom::Regex { .. }) || matches!(need_atom, DataAtom::Regex { .. }) {
        return if atoms_overlap(out_atom, need_atom) {
            Some(Vec::new())
        } else {
            None
        };
    }
    let out_pat = out_atom.path_pattern()?;
    let need_pat = need_atom.path_pattern()?;

    // Unique binding names -> PROBE, then match the producer against the probed
    // need; bindings whose value still contains the probe are unconstrained.
    let mut names: Vec<String> = Vec::new();
    for n in &need_pat.binding_names {
        if !names.contains(n) {
            names.push(n.clone());
        }
    }
    let probe_binds: Vec<(String, String)> =
        names.into_iter().map(|n| (n, PROBE.to_string())).collect();
    let probed = need_pat.substitute(&probe_binds);

    if let Some(matched) = out_pat.matches(&probed) {
        return Some(matched.into_iter().filter(|(_, v)| !v.contains(PROBE)).collect());
    }
    if overlaps(&out_pat, &need_pat) {
        Some(Vec::new())
    } else {
        None
    }
}

pub fn select_actions(actions: &[Action], targets: &[String]) -> Result<Vec<Action>, String> {
    let mut queue: VecDeque<DataAtom> = targets
        .iter()
        .map(|t| DataAtom::File { path: t.clone(), content_key: None })
        .collect();
    let mut seen: HashSet<String> = queue.iter().map(|a| a.dedup_key()).collect();
    let mut selected: BTreeMap<(usize, Vec<(String, String)>), Action> = BTreeMap::new();

    while let Some(need) = queue.pop_front() {
        let mut candidates: Vec<(usize, Vec<(String, String)>)> = Vec::new();
        for (index, action) in actions.iter().enumerate() {
            for out_atom in action.output_atoms() {
                if let Some(bindings) = extract_bindings(&out_atom, &need) {
                    candidates.push((index, bindings));
                }
            }
        }
        if candidates.is_empty() {
            continue; // a source file, not something an action produces
        }
        let best = candidates.iter().map(|(_, b)| specificity(b)).min().unwrap();
        for (index, bindings) in &candidates {
            if specificity(bindings) > best {
                continue; // shortest stem wins
            }
            let mut sorted_bindings = bindings.clone();
            sorted_bindings.sort();
            let key = (*index, sorted_bindings);
            if selected.contains_key(&key) {
                continue;
            }
            let mut constrained = actions[*index].clone();
            if !bindings.is_empty() && constrained.is_expandable() {
                constrained = constrained.substitute(bindings);
            }
            for atom in constrained.input_atoms() {
                let k = atom.dedup_key();
                if seen.insert(k) {
                    queue.push_back(atom);
                }
            }
            selected.insert(key, constrained);
        }
    }

    if selected.is_empty() {
        return Err(format!("no action produces any of: {}", targets.join(", ")));
    }
    Ok(selected.into_values().collect())
}
