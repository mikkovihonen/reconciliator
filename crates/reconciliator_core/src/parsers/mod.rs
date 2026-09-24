pub mod marcxml;
pub mod turtle;

use pyo3::prelude::*;

#[pyfunction]
pub fn py_parse_marc_xml_str(xml: &str) -> Vec<marcxml::MarcRecordData> {
    marcxml::parse_marc_xml_str(xml)
}

#[pyfunction]
#[pyo3(signature = (file_path, use_pigz=true, pigz_threads=4, pigz_path=None))]
pub fn py_parse_marc_file(
    file_path: &str,
    use_pigz: bool,
    pigz_threads: usize,
    pigz_path: Option<&str>,
) -> PyResult<Vec<marcxml::MarcRecordData>> {
    marcxml::parse_marc_file(file_path, use_pigz, pigz_threads, pigz_path)
        .map_err(pyo3::exceptions::PyIOError::new_err)
}

pub fn register_submodule(parent_module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = parent_module.py();
    let submod = PyModule::new(py, "parsers")?;

    submod.add_class::<marcxml::MarcSubfield>()?;
    submod.add_class::<marcxml::MarcDatafield>()?;
    submod.add_class::<marcxml::MarcRecordData>()?;
    submod.add_function(wrap_pyfunction!(py_parse_marc_xml_str, &submod)?)?;
    submod.add_function(wrap_pyfunction!(py_parse_marc_file, &submod)?)?;

    submod.add_class::<turtle::RawVocabConcept>()?;
    submod.add_class::<turtle::RawConceptMapping>()?;
    submod.add_class::<turtle::RawHierarchyEdge>()?;
    submod.add_function(wrap_pyfunction!(turtle::py_parse_turtle_content, &submod)?)?;
    submod.add_function(wrap_pyfunction!(turtle::py_parse_turtle_file, &submod)?)?;

    parent_module.add_submodule(&submod)?;
    py.import("sys")?
        .getattr("modules")?
        .set_item("reconciliator._core.parsers", &submod)?;
    Ok(())
}
