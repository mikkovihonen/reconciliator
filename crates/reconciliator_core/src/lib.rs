pub mod clustering;
pub mod normalizers;
pub mod parsers;
pub mod rerank;

use pyo3::prelude::*;

/// Minimal verification function for Phase 0.
#[pyfunction]
fn rust_ping() -> &'static str {
    "pong from rust"
}

/// Root PyO3 module for reconciliator._core.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rust_ping, m)?)?;
    normalizers::register_submodule(m)?;
    clustering::register_submodule(m)?;
    parsers::register_submodule(m)?;
    rerank::register_submodule(m)?;
    Ok(())
}
