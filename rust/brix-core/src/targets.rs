//! Target selection: the subset (possibly binding-constrained) of actions
//! needed to produce the requested outputs. Demand-driven, on the logical
//! graph, before planning. Mirrors `src/brix/targets.py` — including the
//! shortest-stem rule (make's) that keeps suffix pipelines from diverging.

use std::collections::{BTreeMap, HashSet, VecDeque};

use crate::actions::Action;
use crate::data::{fill_named_groups, DataAtom};
use crate::patterns::overlaps;
use crate::planner::atoms_overlap;
use crate::spec::ForcedOrder;

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

// -- collection membership (static) -----------------------------------------
// Selection can extract bindings that satisfy an action's output template while
// naming something the action's collection would never bind (a nested video
// path binds a render rule's `{video}` to a bare container name the scene regex
// refuses). `specificity` would then pick that collection-blind candidate over
// the right one and cache the wrong artifact over the right one. So before
// ranking, drop any candidate whose bindings a collection in its expansion
// spine *provably* refuses. Purely static (no workspace): real-filesystem
// membership is the executor's job, and resolving it here would break the
// selection/planner layering. One-sided — reject only on a proof of
// non-membership; anything undecidable keeps the candidate. Mirrors
// `_bindings_refused` in `src/brix/targets.py`.

const REGEX_META: &str = ".^$*+?{}[]()|";

/// The single concrete string a fully-literal regex matches (collapsing `\x`
/// escapes), or `None` if any unescaped metacharacter remains.
fn literal_of(pattern: &str) -> Option<String> {
    let chars: Vec<char> = pattern.chars().collect();
    let n = chars.len();
    let mut out = String::new();
    let mut i = 0;
    while i < n {
        if chars[i] == '\\' {
            if i + 1 >= n {
                return None;
            }
            out.push(chars[i + 1]);
            i += 2;
            continue;
        }
        if REGEX_META.contains(chars[i]) {
            return None;
        }
        out.push(chars[i]);
        i += 1;
    }
    Some(out)
}

/// `Some(true)`/`Some(false)` if provable that the regex atom does/does not
/// bind these values; `None` if undecidable in this engine (keep the
/// candidate). A collection regex this build's engine cannot even compile
/// (e.g. lookaround) is undecidable, never a rejection.
fn regex_admits(atom: &DataAtom, bindings: &[(String, String)]) -> Option<bool> {
    let DataAtom::Regex { pattern } = atom else {
        return None;
    };
    // Compile the collection regex exactly as `regex_match` does (fancy-regex,
    // so lookaround collections are decidable here too); a pattern that will
    // not compile tells us nothing about membership.
    let compiled = fancy_regex::Regex::new(&format!(r"\A(?:{pattern})\z")).ok()?;
    let names: Vec<String> = compiled.capture_names().flatten().map(String::from).collect();
    let relevant: Vec<(String, String)> =
        bindings.iter().filter(|(k, _)| names.contains(k)).cloned().collect();
    if relevant.is_empty() {
        return None; // regex binds none of these names — nothing to prove
    }
    let (filled, all_pinned) = fill_named_groups(pattern, &relevant)?;
    if !all_pinned {
        return None; // a group we could not pin — no single probe path exists
    }
    let path = literal_of(&filled)?; // residual regex structure — do not guess
    match atom.regex_match(&path) {
        None => Some(false), // the collection regex refuses this exact path
        Some(matched) => {
            // Matches, but must reproduce the same values — the regex may bind a
            // name differently (a container name absorbed into a nested group).
            let same = relevant
                .iter()
                .all(|(k, v)| matched.iter().any(|(mk, mv)| mk == k && mv == v));
            if same {
                Some(true)
            } else {
                None
            }
        }
    }
}

/// A value is a member if *any* collection atom admits it; so refuse only when
/// every checkable atom provably rejects.
fn collection_refuses(collection: &[DataAtom], bindings: &[(String, String)]) -> bool {
    let verdicts: Vec<Option<bool>> = collection
        .iter()
        .filter(|a| matches!(a, DataAtom::Regex { .. }))
        .map(|a| regex_admits(a, bindings))
        .collect();
    !verdicts.is_empty() && verdicts.iter().all(|v| *v == Some(false))
}

/// True if a collection anywhere in `action`'s expansion spine provably refuses
/// `bindings` — i.e. the target names a non-member and this candidate is
/// collection-blind.
fn bindings_refused(action: &Action, bindings: &[(String, String)]) -> bool {
    let mut current = action;
    while let Action::Expand { collection, child, .. } = current {
        if collection_refuses(collection, bindings) {
            return true;
        }
        current = child;
    }
    false
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

pub(crate) fn select_actions_with_origins(
    actions: &[Action],
    targets: &[String],
    forced_orders: &[ForcedOrder],
) -> Result<Vec<(usize, Action)>, String> {
    let mut queue: VecDeque<DataAtom> = targets
        .iter()
        .map(|t| DataAtom::File { path: t.clone(), content_key: None })
        .collect();
    let mut seen: HashSet<String> = queue.iter().map(|a| a.dedup_key()).collect();
    let mut selected: BTreeMap<(usize, Vec<(String, String)>), Action> = BTreeMap::new();

    loop {
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
                continue;
            }
            // Drop collection-blind candidates: a match whose bindings a nested
            // expansion's collection provably refuses is not a real producer of
            // this target, and must not win on `specificity`.
            candidates.retain(|(index, bindings)| !bindings_refused(&actions[*index], bindings));
            if candidates.is_empty() {
                continue;
            }
            let best = candidates.iter().map(|(_, b)| specificity(b)).min().unwrap();
            for (index, bindings) in &candidates {
                if specificity(bindings) > best {
                    continue;
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

        let origins: HashSet<usize> = selected.keys().map(|(index, _)| *index).collect();
        let mut added = false;
        for order in forced_orders {
            if origins.contains(&order.after) && !origins.contains(&order.before) {
                let action = actions
                    .get(order.before)
                    .ok_or_else(|| "forced order action index is out of range".to_string())?
                    .clone();
                for atom in action.input_atoms() {
                    if seen.insert(atom.dedup_key()) {
                        queue.push_back(atom);
                    }
                }
                selected.insert((order.before, Vec::new()), action);
                added = true;
            }
        }
        if !added && queue.is_empty() {
            break;
        }
    }

    if selected.is_empty() {
        return Err(format!("no action produces any of: {}", targets.join(", ")));
    }
    Ok(selected
        .into_iter()
        .map(|((origin, _), action)| (origin, action))
        .collect())
}

pub fn select_actions(actions: &[Action], targets: &[String]) -> Result<Vec<Action>, String> {
    Ok(select_actions_with_origins(actions, targets, &[])?
        .into_iter()
        .map(|(_, action)| action)
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn regex(pattern: &str) -> DataAtom {
        DataAtom::Regex { pattern: pattern.to_string() }
    }

    fn b(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[test]
    fn regex_admits_provable_reject_and_accept() {
        // A scene must be all-digits; a value with letters can never be bound.
        let scenes = regex(r"src/(?P<video>[^/]+)/(?P<scene>[0-9]+)\.py");
        assert_eq!(regex_admits(&scenes, &b(&[("video", "a"), ("scene", "02x")])), Some(false));
        assert_eq!(regex_admits(&scenes, &b(&[("video", "a"), ("scene", "02")])), Some(true));
    }

    #[test]
    fn regex_admits_handles_lookaround() {
        // fancy-regex compiles the animation build's negative-lookahead
        // container guard, so the collection-blind case is now *decidable* in
        // the Rust engine, not merely kept-conservative. `rights/` is a
        // container: the scene regex refuses video="rights" + a scene...
        let scenes = regex(
            r"src/videos/(?P<video>(?:rights)/[0-9][^/]*|(?!(?:rights)/)[^/]+)/(?P<scene>[0-9][^/]*)\.py",
        );
        assert_eq!(
            regex_admits(&scenes, &b(&[("video", "rights"), ("scene", "02_islandEmperor")])),
            Some(false),
        );
        // ...but a genuinely-nested member is admitted.
        assert_eq!(
            regex_admits(&scenes, &b(&[("video", "rights/02_islandEmperor"), ("scene", "00_title")])),
            Some(true),
        );
    }

    #[test]
    fn regex_admits_conservative_when_unprovable() {
        // A name the collection does not bind -> nothing to prove.
        let plain = regex(r"src/(?P<video>[^/]+)\.py");
        assert_eq!(regex_admits(&plain, &b(&[("scene", "02")])), None);
        // A pattern that will not compile at all -> undecidable, not a reject.
        let broken = regex(r"src/(?P<video>[a-z)\.py");
        assert_eq!(regex_admits(&broken, &b(&[("video", "x")])), None);
        // A non-regex atom is never a reject.
        assert_eq!(
            regex_admits(&DataAtom::Glob { template: "x".into(), content_key: None, exclude: vec![] }, &b(&[("video", "x")])),
            None
        );
    }

    #[test]
    fn bindings_refused_walks_the_spine() {
        let videos = regex(r"src/(?P<video>[^/]+)\.py");
        let scenes = regex(r"src/(?P<video>[^/]+)/(?P<scene>[0-9]+)\.py");
        let render = Action::Expand {
            element: "video".into(),
            collection: vec![videos.clone()],
            child: Box::new(Action::Expand {
                element: "scene".into(),
                collection: vec![scenes],
                child: Box::new(Action::Delete { paths: vec![] }),
            }),
        };
        // The scene level refuses a non-numeric scene, even though the video
        // level would admit it.
        assert!(bindings_refused(&render, &b(&[("video", "a"), ("scene", "02x")])));
        assert!(!bindings_refused(&render, &b(&[("video", "a"), ("scene", "02")])));
    }
}
