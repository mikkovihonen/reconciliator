pub mod blocking;
pub mod identifiers;
pub mod linguistic;

use pyo3::prelude::*;

#[pyfunction]
#[pyo3(signature = (s, preserved_diacritics=None))]
pub fn py_fold_diacritics(s: &str, preserved_diacritics: Option<&str>) -> String {
    linguistic::fold_diacritics(s, preserved_diacritics)
}

#[pyfunction]
pub fn py_strip_biographical_dates(s: &str) -> String {
    linguistic::strip_biographical_dates(s)
}

#[pyfunction]
#[pyo3(signature = (s, preserved_diacritics=None))]
pub fn py_fold_label(s: &str, preserved_diacritics: Option<&str>) -> String {
    linguistic::fold_label(s, preserved_diacritics)
}

#[pyfunction]
pub fn py_isbn_10_to_13(isbn10: &str) -> Option<String> {
    identifiers::isbn_10_to_13(isbn10)
}

#[pyfunction]
pub fn py_validate_isbn_13(isbn13: &str) -> bool {
    identifiers::validate_isbn_13(isbn13)
}

#[pyfunction]
pub fn py_clean_alphanumeric(s: &str) -> String {
    identifiers::clean_alphanumeric(s)
}

#[pyfunction]
pub fn py_deterministic_key_hash(key_content: &str) -> String {
    blocking::deterministic_key_hash(key_content)
}

#[pyfunction]
pub fn py_compute_work_blocking_key(author_key: &str, title_stem: &str) -> String {
    blocking::compute_work_blocking_key(author_key, title_stem)
}

#[pyfunction]
#[pyo3(signature = (series_title_stem=None, series_issn=None))]
pub fn py_compute_series_key(
    series_title_stem: Option<&str>,
    series_issn: Option<&str>,
) -> Option<String> {
    blocking::compute_series_key(series_title_stem, series_issn)
}

#[pyfunction]
#[pyo3(signature = (issn=None, year=None, start_page=None, author_surname=None))]
pub fn py_compute_host_citation_key(
    issn: Option<&str>,
    year: Option<&str>,
    start_page: Option<&str>,
    author_surname: Option<&str>,
) -> Option<String> {
    blocking::compute_host_citation_key(issn, year, start_page, author_surname)
}

pub fn register_submodule(parent_module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = parent_module.py();
    let submod = PyModule::new(py, "normalizers")?;

    submod.add_function(wrap_pyfunction!(py_fold_diacritics, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_strip_biographical_dates, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_fold_label, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_isbn_10_to_13, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_validate_isbn_13, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_clean_alphanumeric, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_deterministic_key_hash, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_compute_work_blocking_key, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_compute_series_key, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_compute_host_citation_key, &submod)?)?;

    parent_module.add_submodule(&submod)?;
    py.import("sys")?
        .getattr("modules")?
        .set_item("reconciliator._core.normalizers", &submod)?;
    Ok(())
}
