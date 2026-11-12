// Copyright (C) 2026 Mikko Vihonen <mikko.vihonen@iki.fi>
//
// This program is free software; you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation; either version 2 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License along
// with this program; if not, see <https://www.gnu.org/licenses/>.

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
