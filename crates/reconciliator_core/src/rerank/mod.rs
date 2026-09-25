pub mod bm25;
pub mod discriminators;

use pyo3::prelude::*;

#[pyfunction]
#[pyo3(signature = (query, documents, k1=1.5, b=0.75))]
pub fn py_bm25_score_batch(query: &str, documents: Vec<String>, k1: f64, b: f64) -> Vec<f64> {
    bm25::bm25_score_batch(query, &documents, k1, b)
}

#[pyfunction]
#[pyo3(signature = (text, format_template="osa {num}"))]
pub fn py_normalize_volume(text: &str, format_template: &str) -> Option<String> {
    discriminators::normalize_volume_fast(text, format_template)
}

#[pyfunction]
#[pyo3(signature = (target_title, candidate_title, target_year=None, candidate_year=None))]
pub fn py_check_numeric_title_tokens_compatible(
    target_title: &str,
    candidate_title: &str,
    target_year: Option<i32>,
    candidate_year: Option<i32>,
) -> bool {
    discriminators::check_numeric_title_tokens_compatible(
        target_title,
        candidate_title,
        target_year,
        candidate_year,
    )
}

pub fn register_submodule(parent_module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = parent_module.py();
    let submod = PyModule::new(py, "rerank")?;

    submod.add_function(wrap_pyfunction!(py_bm25_score_batch, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_normalize_volume, &submod)?)?;
    submod.add_function(wrap_pyfunction!(
        py_check_numeric_title_tokens_compatible,
        &submod
    )?)?;

    parent_module.add_submodule(&submod)?;
    py.import("sys")?
        .getattr("modules")?
        .set_item("reconciliator._core.rerank", &submod)?;
    Ok(())
}
