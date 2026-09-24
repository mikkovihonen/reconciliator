use pyo3::prelude::*;
use quick_xml::events::Event;
use quick_xml::reader::Reader;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};

#[pyclass(from_py_object)]
#[derive(Clone, Debug, Default)]
pub struct MarcSubfield {
    #[pyo3(get)]
    pub code: String,
    #[pyo3(get)]
    pub value: String,
}

#[pymethods]
impl MarcSubfield {
    #[new]
    pub fn new(code: String, value: String) -> Self {
        Self { code, value }
    }
}

#[pyclass(from_py_object)]
#[derive(Clone, Debug, Default)]
pub struct MarcDatafield {
    #[pyo3(get)]
    pub tag: String,
    #[pyo3(get)]
    pub ind1: String,
    #[pyo3(get)]
    pub ind2: String,
    #[pyo3(get)]
    pub subfields: Vec<MarcSubfield>,
}

#[pymethods]
impl MarcDatafield {
    #[new]
    pub fn new(tag: String, ind1: String, ind2: String, subfields: Vec<MarcSubfield>) -> Self {
        Self {
            tag,
            ind1,
            ind2,
            subfields,
        }
    }

    pub fn get_subfield(&self, code: &str) -> Option<String> {
        for sf in &self.subfields {
            if sf.code == code {
                return Some(sf.value.clone());
            }
        }
        None
    }

    pub fn get_subfields(&self, code: &str) -> Vec<String> {
        self.subfields
            .iter()
            .filter(|sf| sf.code == code)
            .map(|sf| sf.value.clone())
            .collect()
    }
}

#[pyclass(from_py_object)]
#[derive(Clone, Debug, Default)]
pub struct MarcRecordData {
    #[pyo3(get)]
    pub leader: Option<String>,
    #[pyo3(get)]
    pub controlfields: Vec<(String, String)>,
    #[pyo3(get)]
    pub datafields: Vec<MarcDatafield>,
}

#[pymethods]
impl MarcRecordData {
    pub fn get_controlfield(&self, tag: &str) -> Option<String> {
        for (t, v) in &self.controlfields {
            if t == tag {
                return Some(v.clone());
            }
        }
        None
    }

    pub fn get_datafields(&self, tag: &str) -> Vec<MarcDatafield> {
        self.datafields
            .iter()
            .filter(|df| df.tag == tag)
            .cloned()
            .collect()
    }

    pub fn get_subfield(&self, tag: &str, code: &str) -> Option<String> {
        for df in &self.datafields {
            if df.tag == tag {
                if let Some(v) = df.get_subfield(code) {
                    return Some(v);
                }
            }
        }
        None
    }

    pub fn get_subfields(&self, tag: &str, code: &str) -> Vec<String> {
        let mut results = Vec::new();
        for df in &self.datafields {
            if df.tag == tag {
                results.extend(df.get_subfields(code));
            }
        }
        results
    }
}

/// Parse MARCXML records from any BufRead reader.
pub fn parse_marc_stream<R: BufRead>(mut reader: Reader<R>) -> Vec<MarcRecordData> {
    let mut records = Vec::new();
    let mut buf = Vec::with_capacity(4096);

    let mut current_record: Option<MarcRecordData> = None;
    let mut current_datafield: Option<MarcDatafield> = None;
    let mut current_tag = String::new();
    let mut current_code = String::new();
    let mut in_leader = false;
    let mut in_controlfield = false;
    let mut in_subfield = false;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let local_name = e.local_name();
                match local_name.as_ref() {
                    b"record" => {
                        current_record = Some(MarcRecordData::default());
                    }
                    b"leader" => {
                        in_leader = true;
                    }
                    b"controlfield" => {
                        in_controlfield = true;
                        current_tag.clear();
                        for attr in e.attributes().flatten() {
                            if attr.key.local_name().as_ref() == b"tag" {
                                current_tag = String::from_utf8_lossy(&attr.value).to_string();
                            }
                        }
                    }
                    b"datafield" => {
                        let mut tag = String::new();
                        let mut ind1 = " ".to_string();
                        let mut ind2 = " ".to_string();
                        for attr in e.attributes().flatten() {
                            match attr.key.local_name().as_ref() {
                                b"tag" => tag = String::from_utf8_lossy(&attr.value).to_string(),
                                b"ind1" => ind1 = String::from_utf8_lossy(&attr.value).to_string(),
                                b"ind2" => ind2 = String::from_utf8_lossy(&attr.value).to_string(),
                                _ => {}
                            }
                        }
                        current_datafield = Some(MarcDatafield {
                            tag,
                            ind1,
                            ind2,
                            subfields: Vec::new(),
                        });
                    }
                    b"subfield" => {
                        in_subfield = true;
                        current_code.clear();
                        for attr in e.attributes().flatten() {
                            if attr.key.local_name().as_ref() == b"code" {
                                current_code = String::from_utf8_lossy(&attr.value).to_string();
                            }
                        }
                    }
                    _ => {}
                }
            }
            Ok(Event::End(e)) => {
                let local_name = e.local_name();
                match local_name.as_ref() {
                    b"record" => {
                        if let Some(rec) = current_record.take() {
                            records.push(rec);
                        }
                    }
                    b"leader" => in_leader = false,
                    b"controlfield" => in_controlfield = false,
                    b"datafield" => {
                        if let (Some(rec), Some(df)) =
                            (current_record.as_mut(), current_datafield.take())
                        {
                            rec.datafields.push(df);
                        }
                    }
                    b"subfield" => in_subfield = false,
                    _ => {}
                }
            }
            Ok(Event::Text(e)) => {
                let text = e.unescape().map(|c| c.into_owned()).unwrap_or_default();
                if in_leader {
                    if let Some(rec) = current_record.as_mut() {
                        rec.leader = Some(text);
                    }
                } else if in_controlfield {
                    if let Some(rec) = current_record.as_mut() {
                        rec.controlfields.push((current_tag.clone(), text));
                    }
                } else if in_subfield {
                    if let Some(df) = current_datafield.as_mut() {
                        df.subfields.push(MarcSubfield {
                            code: current_code.clone(),
                            value: text,
                        });
                    }
                }
            }
            Ok(Event::Eof) => break,
            Err(_) => break,
            _ => {}
        }
        buf.clear();
    }

    records
}

/// Parse MARCXML from an XML string or bytes in memory.
pub fn parse_marc_xml_str(xml: &str) -> Vec<MarcRecordData> {
    let reader = Reader::from_str(xml);
    parse_marc_stream(reader)
}

/// Stream and parse MARCXML records from a file (optionally with pigz multi-threaded decompression).
pub fn parse_marc_file(
    file_path: &str,
    use_pigz: bool,
    pigz_threads: usize,
    pigz_path: Option<&str>,
) -> Result<Vec<MarcRecordData>, String> {
    let is_gzipped = file_path.ends_with(".gz");

    if is_gzipped && use_pigz {
        let pigz_bin = pigz_path.unwrap_or("pigz");
        let threads_arg = if pigz_threads > 0 {
            pigz_threads.to_string()
        } else {
            "4".to_string()
        };

        if let Ok(mut child) = Command::new(pigz_bin)
            .args(["-dc", "-p", &threads_arg, file_path])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
        {
            if let Some(stdout) = child.stdout.take() {
                let buf_reader = BufReader::with_capacity(128 * 1024, stdout);
                let reader = Reader::from_reader(buf_reader);
                let records = parse_marc_stream(reader);
                let _ = child.wait();
                return Ok(records);
            }
        }
    }

    // Fallback: regular file open
    let file =
        File::open(file_path).map_err(|e| format!("Failed to open file {}: {}", file_path, e))?;
    let buf_reader = BufReader::with_capacity(128 * 1024, file);
    let reader = Reader::from_reader(buf_reader);
    Ok(parse_marc_stream(reader))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE_XML: &str = r#"<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>01234nam a2200349 c 4500</leader>
  <controlfield tag="001">123456</controlfield>
  <controlfield tag="003">FI-MELI</controlfield>
  <controlfield tag="008">950101s1995 fi</controlfield>
  <datafield tag="020" ind1=" " ind2=" "><subfield code="a">978-951-0-12345-6</subfield></datafield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Virtanen, Ville,</subfield>
    <subfield code="d">1950-</subfield>
  </datafield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Testikirja :</subfield>
    <subfield code="b">alaotsikko /</subfield>
  </datafield>
</record>"#;

    #[test]
    fn test_parse_sample_marc_xml() {
        let records = parse_marc_xml_str(SAMPLE_XML);
        assert_eq!(records.len(), 1);

        let rec = &records[0];
        assert_eq!(rec.leader.as_deref(), Some("01234nam a2200349 c 4500"));
        assert_eq!(rec.get_controlfield("001").as_deref(), Some("123456"));
        assert_eq!(rec.get_controlfield("003").as_deref(), Some("FI-MELI"));

        assert_eq!(
            rec.get_subfield("020", "a").as_deref(),
            Some("978-951-0-12345-6")
        );
        assert_eq!(
            rec.get_subfield("100", "a").as_deref(),
            Some("Virtanen, Ville,")
        );
        assert_eq!(
            rec.get_subfield("245", "a").as_deref(),
            Some("Testikirja :")
        );
        assert_eq!(
            rec.get_subfield("245", "b").as_deref(),
            Some("alaotsikko /")
        );
    }
}
