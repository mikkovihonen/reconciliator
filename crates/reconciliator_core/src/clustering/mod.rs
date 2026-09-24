pub mod lcs;
pub mod union_find;

use pyo3::prelude::*;
use std::collections::HashMap;

#[pyfunction]
pub fn py_lcs_length(seq1: Vec<String>, seq2: Vec<String>) -> usize {
    lcs::lcs_length(&seq1, &seq2)
}

#[pyfunction]
pub fn py_jaccard_similarity(c1: Vec<String>, c2: Vec<String>) -> f64 {
    lcs::jaccard_similarity(&c1, &c2)
}

#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (
    c1,
    c2,
    is_component1=false,
    is_component2=false,
    min_constituents=2,
    jaccard_veto_threshold=0.30,
    jaccard_merge_threshold=0.80,
    sequence_alignment=true,
    lcs_threshold=0.70,
    merge_bonus_score=0.95
))]
pub fn py_evaluate_aggregate_overlap(
    c1: Vec<String>,
    c2: Vec<String>,
    is_component1: bool,
    is_component2: bool,
    min_constituents: usize,
    jaccard_veto_threshold: f64,
    jaccard_merge_threshold: f64,
    sequence_alignment: bool,
    lcs_threshold: f64,
    merge_bonus_score: f64,
) -> Option<(f64, String)> {
    lcs::evaluate_aggregate_overlap(
        &c1,
        &c2,
        is_component1,
        is_component2,
        min_constituents,
        jaccard_veto_threshold,
        jaccard_merge_threshold,
        sequence_alignment,
        lcs_threshold,
        merge_bonus_score,
    )
    .map(|(score, ev)| (score, ev.to_string()))
}

#[pyfunction]
pub fn py_cluster_connected_components(
    num_nodes: usize,
    edges: Vec<(usize, usize)>,
) -> HashMap<usize, Vec<usize>> {
    let mut uf = union_find::IntUnionFind::new(num_nodes);
    for (u, v) in edges {
        if u < num_nodes && v < num_nodes {
            uf.union(u, v);
        }
    }
    uf.get_components().into_iter().collect()
}

pub fn register_submodule(parent_module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = parent_module.py();
    let submod = PyModule::new(py, "clustering")?;

    submod.add_function(wrap_pyfunction!(py_lcs_length, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_jaccard_similarity, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_evaluate_aggregate_overlap, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_cluster_connected_components, &submod)?)?;

    parent_module.add_submodule(&submod)?;
    py.import("sys")?
        .getattr("modules")?
        .set_item("reconciliator._core.clustering", &submod)?;
    Ok(())
}
