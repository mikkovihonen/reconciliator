use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;

#[pyclass(get_all, set_all, from_py_object)]
#[derive(Clone, Debug, Default)]
pub struct RawVocabConcept {
    pub uri: String,
    pub vocabulary: String,
    pub concept_id: String,
    pub pref_labels: HashMap<String, String>,
    pub alt_labels: HashMap<String, Vec<String>>,
    pub is_deprecated: bool,
    pub target_field: Option<String>,
}

#[pymethods]
impl RawVocabConcept {
    #[new]
    #[pyo3(signature = (uri, vocabulary, concept_id, pref_labels=None, alt_labels=None, is_deprecated=false, target_field=None))]
    fn new(
        uri: String,
        vocabulary: String,
        concept_id: String,
        pref_labels: Option<HashMap<String, String>>,
        alt_labels: Option<HashMap<String, Vec<String>>>,
        is_deprecated: bool,
        target_field: Option<String>,
    ) -> Self {
        Self {
            uri,
            vocabulary,
            concept_id,
            pref_labels: pref_labels.unwrap_or_default(),
            alt_labels: alt_labels.unwrap_or_default(),
            is_deprecated,
            target_field,
        }
    }
}

#[pyclass(get_all, set_all, from_py_object)]
#[derive(Clone, Debug, Default)]
pub struct RawConceptMapping {
    pub legacy_uri: String,
    pub legacy_vocab: String,
    pub legacy_id: String,
    pub target_uri: String,
    pub target_vocab: String,
    pub target_id: String,
    pub mapping_type: String,
    pub target_field: String,
    pub legacy_labels: HashMap<String, String>,
}

#[pymethods]
impl RawConceptMapping {
    #[new]
    #[pyo3(signature = (legacy_uri, legacy_vocab, legacy_id, target_uri, target_vocab, target_id, mapping_type, target_field, legacy_labels=None))]
    fn new(
        legacy_uri: String,
        legacy_vocab: String,
        legacy_id: String,
        target_uri: String,
        target_vocab: String,
        target_id: String,
        mapping_type: String,
        target_field: String,
        legacy_labels: Option<HashMap<String, String>>,
    ) -> Self {
        Self {
            legacy_uri,
            legacy_vocab,
            legacy_id,
            target_uri,
            target_vocab,
            target_id,
            mapping_type,
            target_field,
            legacy_labels: legacy_labels.unwrap_or_default(),
        }
    }
}

#[pyclass(get_all, set_all, from_py_object)]
#[derive(Clone, Debug, Default)]
pub struct RawHierarchyEdge {
    pub vocabulary: String,
    pub concept_id: String,
    pub broader_concept_id: String,
    pub relation_type: String,
}

#[pymethods]
impl RawHierarchyEdge {
    #[new]
    #[pyo3(signature = (vocabulary, concept_id, broader_concept_id, relation_type="broader".to_string()))]
    fn new(
        vocabulary: String,
        concept_id: String,
        broader_concept_id: String,
        relation_type: String,
    ) -> Self {
        Self {
            vocabulary,
            concept_id,
            broader_concept_id,
            relation_type,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Token<'a> {
    Dot,
    Semicolon,
    Comma,
    OpenBracket,
    CloseBracket,
    Iri(&'a str),
    PrefixedOrKeyword(&'a str),
    Literal {
        value: String,
        lang: Option<&'a str>,
    },
}

struct TurtleTokenizer<'a> {
    chars: &'a str,
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> TurtleTokenizer<'a> {
    fn new(source: &'a str) -> Self {
        Self {
            chars: source,
            bytes: source.as_bytes(),
            pos: 0,
        }
    }

    fn skip_whitespace_and_comments(&mut self) {
        while self.pos < self.bytes.len() {
            match self.bytes[self.pos] {
                b' ' | b'\t' | b'\r' | b'\n' => {
                    self.pos += 1;
                }
                b'#' => {
                    // Skip comment till end of line
                    self.pos += 1;
                    while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
                        self.pos += 1;
                    }
                }
                _ => break,
            }
        }
    }

    fn next_token(&mut self) -> Option<Token<'a>> {
        self.skip_whitespace_and_comments();
        if self.pos >= self.bytes.len() {
            return None;
        }

        let b = self.bytes[self.pos];
        match b {
            b'.' => {
                self.pos += 1;
                Some(Token::Dot)
            }
            b';' => {
                self.pos += 1;
                Some(Token::Semicolon)
            }
            b',' => {
                self.pos += 1;
                Some(Token::Comma)
            }
            b'[' => {
                self.pos += 1;
                Some(Token::OpenBracket)
            }
            b']' => {
                self.pos += 1;
                Some(Token::CloseBracket)
            }
            b'<' => {
                // Parse IRI <...>
                self.pos += 1;
                let start = self.pos;
                while self.pos < self.bytes.len() && self.bytes[self.pos] != b'>' {
                    self.pos += 1;
                }
                let iri_slice = &self.chars[start..self.pos];
                if self.pos < self.bytes.len() {
                    self.pos += 1; // skip '>'
                }
                Some(Token::Iri(iri_slice))
            }
            b'"' | b'\'' => {
                let quote_byte = b;
                // Check if triple quote
                let is_triple = self.pos + 2 < self.bytes.len()
                    && self.bytes[self.pos + 1] == quote_byte
                    && self.bytes[self.pos + 2] == quote_byte;

                let mut val = String::new();
                if is_triple {
                    self.pos += 3;
                    while self.pos < self.bytes.len() {
                        if self.bytes[self.pos] == quote_byte
                            && self.pos + 2 < self.bytes.len()
                            && self.bytes[self.pos + 1] == quote_byte
                            && self.bytes[self.pos + 2] == quote_byte
                        {
                            self.pos += 3;
                            break;
                        }
                        if self.bytes[self.pos] == b'\\' && self.pos + 1 < self.bytes.len() {
                            self.pos += 1;
                            match self.bytes[self.pos] {
                                b'n' => val.push('\n'),
                                b'r' => val.push('\r'),
                                b't' => val.push('\t'),
                                b'\\' => val.push('\\'),
                                b'"' => val.push('"'),
                                b'\'' => val.push('\''),
                                other => {
                                    val.push('\\');
                                    val.push(other as char);
                                }
                            }
                        } else {
                            // Find next char
                            let ch = self.chars[self.pos..].chars().next().unwrap();
                            val.push(ch);
                            self.pos += ch.len_utf8() - 1;
                        }
                        self.pos += 1;
                    }
                } else {
                    self.pos += 1;
                    while self.pos < self.bytes.len() {
                        if self.bytes[self.pos] == quote_byte {
                            self.pos += 1;
                            break;
                        }
                        if self.bytes[self.pos] == b'\\' && self.pos + 1 < self.bytes.len() {
                            self.pos += 1;
                            match self.bytes[self.pos] {
                                b'n' => val.push('\n'),
                                b'r' => val.push('\r'),
                                b't' => val.push('\t'),
                                b'\\' => val.push('\\'),
                                b'"' => val.push('"'),
                                b'\'' => val.push('\''),
                                other => {
                                    val.push('\\');
                                    val.push(other as char);
                                }
                            }
                        } else {
                            let ch = self.chars[self.pos..].chars().next().unwrap();
                            val.push(ch);
                            self.pos += ch.len_utf8() - 1;
                        }
                        self.pos += 1;
                    }
                }

                // Check for @lang or ^^<datatype>
                let mut lang: Option<&'a str> = None;
                if self.pos < self.bytes.len() && self.bytes[self.pos] == b'@' {
                    self.pos += 1;
                    let l_start = self.pos;
                    while self.pos < self.bytes.len()
                        && (self.bytes[self.pos].is_ascii_alphanumeric()
                            || self.bytes[self.pos] == b'-')
                    {
                        self.pos += 1;
                    }
                    lang = Some(&self.chars[l_start..self.pos]);
                } else if self.pos + 1 < self.bytes.len()
                    && self.bytes[self.pos] == b'^'
                    && self.bytes[self.pos + 1] == b'^'
                {
                    self.pos += 2;
                    // Skip datatype IRI or prefixed name
                    if self.pos < self.bytes.len() && self.bytes[self.pos] == b'<' {
                        self.pos += 1;
                        while self.pos < self.bytes.len() && self.bytes[self.pos] != b'>' {
                            self.pos += 1;
                        }
                        if self.pos < self.bytes.len() {
                            self.pos += 1;
                        }
                    } else {
                        while self.pos < self.bytes.len()
                            && !self.bytes[self.pos].is_ascii_whitespace()
                            && self.bytes[self.pos] != b';'
                            && self.bytes[self.pos] != b','
                            && self.bytes[self.pos] != b'.'
                            && self.bytes[self.pos] != b']'
                        {
                            self.pos += 1;
                        }
                    }
                }

                Some(Token::Literal { value: val, lang })
            }
            _ => {
                // Word (prefixed name, keyword like 'a', or directive)
                let start = self.pos;
                while self.pos < self.bytes.len() {
                    let c = self.bytes[self.pos];
                    if c.is_ascii_whitespace()
                        || c == b';'
                        || c == b','
                        || c == b'.'
                        || c == b'['
                        || c == b']'
                        || c == b'('
                        || c == b')'
                    {
                        break;
                    }
                    self.pos += 1;
                }
                let word = &self.chars[start..self.pos];
                Some(Token::PrefixedOrKeyword(word))
            }
        }
    }
}

pub fn extract_concept_id(raw: &str) -> String {
    let s = raw.trim();
    if s.is_empty() {
        return String::new();
    }
    // 1. (FI-YSO)p2591
    if s.starts_with('(') {
        if let Some(pos) = s.find(')') {
            let tail = s[pos + 1..].trim();
            if !tail.is_empty() {
                return tail.to_string();
            }
        }
    }
    // 2. Fragment #
    if let Some(pos) = s.rfind('#') {
        let frag = s[pos + 1..].trim();
        if !frag.is_empty() {
            return frag.to_string();
        }
    }
    // 3. URN
    if s.to_ascii_lowercase().contains("urn:") {
        let trimmed = s.trim_end_matches('/');
        if let Some(pos) = trimmed.rfind(':') {
            let tail = trimmed[pos + 1..].trim();
            if !tail.is_empty() {
                return tail.to_string();
            }
        }
    }
    // 4. Standard path /
    let trimmed_slash = s.trim_end_matches('/');
    if let Some(pos) = trimmed_slash.rfind('/') {
        let tail = trimmed_slash[pos + 1..].trim();
        if !tail.is_empty() {
            return tail.to_string();
        }
    }
    // 5. Colon :
    let trimmed_colon = s.trim_end_matches(':');
    if let Some(pos) = trimmed_colon.rfind(':') {
        let tail = trimmed_colon[pos + 1..].trim();
        if !tail.is_empty() {
            return tail.to_string();
        }
    }
    s.to_string()
}

pub fn detect_vocab(
    uri: &str,
    patterns: &[(String, String)],
    default_vocab: &str,
) -> String {
    let lower = uri.to_ascii_lowercase();

    // Check custom and standard patterns first
    for (pat, voc) in patterns {
        if lower.contains(pat) {
            return voc.clone();
        }
    }

    // Check URN Finnish authority namespace e.g. urn:nbn:fi:au:kaunokki:k99
    if let Some(idx) = lower.find("au:") {
        let after = &lower[idx + 3..];
        if let Some(colon) = after.find(':') {
            let voc = &after[..colon];
            if !voc.is_empty() && voc.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '-') {
                return voc.to_string();
            }
        }
    }

    // Fallback: examine last non-numeric segment of path before fragment
    let clean = if let Some(idx) = uri.find('#') {
        &uri[..idx]
    } else {
        uri
    };
    for seg in clean.trim_end_matches('/').rsplit('/') {
        let filtered: String = seg
            .chars()
            .filter(|c| c.is_alphanumeric() || *c == '_' || *c == '-')
            .collect();
        let is_letter_digits = filtered.len() > 1
            && filtered.chars().next().map_or(false, |c| c.is_alphabetic())
            && filtered.chars().skip(1).all(|c| c.is_ascii_digit());
        let is_all_digits = filtered.chars().all(|c| c.is_ascii_digit());
        if !filtered.is_empty() && !is_all_digits && !is_letter_digits && filtered.len() > 2 {
            return filtered.to_ascii_lowercase();
        }
    }
    if !default_vocab.is_empty() {
        default_vocab.to_ascii_lowercase()
    } else {
        "vocab".to_string()
    }
}

fn expand_term(term: &str, prefixes: &HashMap<String, String>) -> String {
    if term.starts_with('<') && term.ends_with('>') && term.len() >= 2 {
        return term[1..term.len() - 1].to_string();
    }
    if term == "a" {
        return "http://www.w3.org/1999/02/22-rdf-syntax-ns#type".to_string();
    }
    if let Some(idx) = term.find(':') {
        let prefix = &term[..idx];
        let local = &term[idx + 1..];
        if let Some(base) = prefixes.get(prefix) {
            return format!("{}{}", base, local);
        }
    }
    term.to_string()
}

fn is_lang_allowed(lang: &str, allowed: Option<&HashSet<String>>) -> bool {
    match allowed {
        None => true,
        Some(set) => {
            if set.contains("*") || set.contains("all") || set.contains("") {
                true
            } else {
                set.contains(&lang.to_ascii_lowercase())
            }
        }
    }
}

pub fn default_vocab_patterns() -> Vec<(String, String)> {
    let mut pats = vec![
        ("onto/yso-aika/".to_string(), "yso-aika".to_string()),
        ("yso-aika".to_string(), "yso-aika".to_string()),
        ("onto/yso-paikat/".to_string(), "yso-paikat".to_string()),
        ("yso-paikat".to_string(), "yso-paikat".to_string()),
        ("onto/yso/".to_string(), "yso".to_string()),
        ("onto/kauno/".to_string(), "kauno".to_string()),
        ("onto/muso/".to_string(), "muso".to_string()),
        ("au:seko".to_string(), "seko".to_string()),
        ("au:slm".to_string(), "slm".to_string()),
        ("onto/ysa/".to_string(), "ysa".to_string()),
        ("onto/allars/".to_string(), "allars".to_string()),
        ("onto/kaunokki/".to_string(), "kaunokki".to_string()),
        ("onto/bella/".to_string(), "bella".to_string()),
        ("onto/musa/".to_string(), "musa".to_string()),
        ("onto/cilla/".to_string(), "cilla".to_string()),
    ];
    // Sort by pattern length descending for greedy match
    pats.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
    pats
}

#[derive(Default)]
struct SubjectAccumulator {
    is_concept: bool,
    pref_labels: HashMap<String, String>,
    alt_labels: HashMap<String, Vec<String>>,
    in_scheme: Option<String>,
    is_deprecated: bool,
    replacements: Vec<String>,
    exact_matches: Vec<String>,
    broaders: Vec<String>,
    narrowers: Vec<String>,
}

pub fn parse_turtle_content_internal(
    content: &str,
    default_vocab: Option<&str>,
    legacy_thesauri: Option<&HashSet<String>>,
    vocab_target_fields: Option<&HashMap<String, String>>,
    default_target_field: Option<&str>,
    pref_langs: Option<&HashSet<String>>,
    alt_langs: Option<&HashSet<String>>,
    custom_patterns: Option<&[(String, String)]>,
) -> (
    Vec<RawVocabConcept>,
    Vec<RawConceptMapping>,
    Vec<RawHierarchyEdge>,
) {
    let mut prefixes: HashMap<String, String> = HashMap::new();
    let mut tokenizer = TurtleTokenizer::new(content);

    let def_vocab = default_vocab.unwrap_or("vocab");
    let def_target_field = default_target_field.unwrap_or("650");
    let mut patterns = default_vocab_patterns();
    if let Some(custom) = custom_patterns {
        patterns.extend(custom.iter().cloned());
        patterns.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
    }

    let default_legacy: HashSet<String> = [
        "ysa", "allars", "kaunokki", "bella", "musa", "cilla",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let active_legacy = legacy_thesauri.unwrap_or(&default_legacy);

    let mut concepts_map: HashMap<String, SubjectAccumulator> = HashMap::new();
    let mut concept_order: Vec<String> = Vec::new();

    // Loop through statements
    while let Some(tok) = tokenizer.next_token() {
        match tok {
            Token::PrefixedOrKeyword(kw) => {
                let lower_kw = kw.to_ascii_lowercase();
                if lower_kw == "@prefix" || lower_kw == "prefix" {
                    // prefix name
                    let p_name = match tokenizer.next_token() {
                        Some(Token::PrefixedOrKeyword(p)) => p.trim_end_matches(':').to_string(),
                        _ => continue,
                    };
                    // IRI
                    let iri = match tokenizer.next_token() {
                        Some(Token::Iri(i)) => i.to_string(),
                        _ => continue,
                    };
                    // optional dot
                    prefixes.insert(p_name, iri);
                    continue;
                } else if lower_kw == "@base" || lower_kw == "base" {
                    let _ = tokenizer.next_token();
                    continue;
                }

                // It is a Subject!
                let subject_uri = expand_term(kw, &prefixes);
                parse_statement(
                    subject_uri,
                    &mut tokenizer,
                    &prefixes,
                    pref_langs,
                    alt_langs,
                    &mut concepts_map,
                    &mut concept_order,
                );
            }
            Token::Iri(iri) => {
                let subject_uri = iri.to_string();
                parse_statement(
                    subject_uri,
                    &mut tokenizer,
                    &prefixes,
                    pref_langs,
                    alt_langs,
                    &mut concepts_map,
                    &mut concept_order,
                );
            }
            Token::Dot | Token::Semicolon | Token::Comma | Token::CloseBracket => {
                // Stray punctuation, ignore
            }
            Token::OpenBracket => {
                // Blank node at top level, skip till closing bracket
                let mut depth = 1;
                while depth > 0 {
                    match tokenizer.next_token() {
                        Some(Token::OpenBracket) => depth += 1,
                        Some(Token::CloseBracket) => depth -= 1,
                        None => break,
                        _ => {}
                    }
                }
            }
            Token::Literal { .. } => {
                // Unexpected literal subject, ignore
            }
        }
    }

    let mut concepts = Vec::with_capacity(concept_order.len());
    let mut mappings = Vec::new();
    let mut hierarchy_edges = Vec::new();

    for uri in concept_order {
        let acc = match concepts_map.remove(&uri) {
            Some(a) => a,
            None => continue,
        };

        if !acc.is_concept {
            continue;
        }

        // Determine vocabulary
        let mut vocab = String::new();
        if let Some(ref scheme) = acc.in_scheme {
            let det = detect_vocab(scheme, &patterns, "");
            if !det.is_empty() && det != "vocab" {
                vocab = det;
            }
        }
        if vocab.is_empty() {
            vocab = detect_vocab(&uri, &patterns, def_vocab);
        }
        if (vocab.is_empty() || vocab == "vocab") && !def_vocab.is_empty() {
            vocab = def_vocab.to_ascii_lowercase();
        }

        let target_field = vocab_target_fields
            .and_then(|m| m.get(&vocab))
            .map(|s| s.as_str())
            .unwrap_or(def_target_field)
            .to_string();

        let concept_id = extract_concept_id(&uri);

        let concept = RawVocabConcept {
            uri: uri.clone(),
            vocabulary: vocab.clone(),
            concept_id: concept_id.clone(),
            pref_labels: acc.pref_labels.clone(),
            alt_labels: acc.alt_labels,
            is_deprecated: acc.is_deprecated,
            target_field: Some(target_field.clone()),
        };
        concepts.push(concept);

        // 1. Deprecated replacement mappings
        for rep in acc.replacements {
            let target_v = detect_vocab(&rep, &patterns, def_vocab);
            let t_field = vocab_target_fields
                .and_then(|m| m.get(&target_v))
                .map(|s| s.as_str())
                .unwrap_or(&target_field)
                .to_string();
            let target_cid = extract_concept_id(&rep);
            mappings.push(RawConceptMapping {
                legacy_uri: uri.clone(),
                legacy_vocab: vocab.clone(),
                legacy_id: concept_id.clone(),
                target_uri: rep,
                target_vocab: target_v,
                target_id: target_cid,
                mapping_type: "deprecated_replacement".to_string(),
                target_field: t_field,
                legacy_labels: acc.pref_labels.clone(),
            });
        }

        // 2. Legacy thesauri mappings (exactMatch)
        for m_uri in acc.exact_matches {
            let match_v = detect_vocab(&m_uri, &patterns, def_vocab);
            let match_cid = extract_concept_id(&m_uri);

            if active_legacy.contains(&match_v) && !active_legacy.contains(&vocab) {
                // Inverted: legacy thesaurus maps into this modern canonical concept
                mappings.push(RawConceptMapping {
                    legacy_uri: m_uri,
                    legacy_vocab: match_v,
                    legacy_id: match_cid,
                    target_uri: uri.clone(),
                    target_vocab: vocab.clone(),
                    target_id: concept_id.clone(),
                    mapping_type: "exact_match".to_string(),
                    target_field: target_field.clone(),
                    legacy_labels: acc.pref_labels.clone(),
                });
            } else if active_legacy.contains(&vocab) && !active_legacy.contains(&match_v) {
                // Direct: current concept is legacy thesaurus mapping to modern concept
                let t_field = vocab_target_fields
                    .and_then(|m| m.get(&match_v))
                    .map(|s| s.as_str())
                    .unwrap_or(def_target_field)
                    .to_string();
                mappings.push(RawConceptMapping {
                    legacy_uri: uri.clone(),
                    legacy_vocab: vocab.clone(),
                    legacy_id: concept_id.clone(),
                    target_uri: m_uri,
                    target_vocab: match_v,
                    target_id: match_cid,
                    mapping_type: "exact_match".to_string(),
                    target_field: t_field,
                    legacy_labels: acc.pref_labels.clone(),
                });
            }
        }

        // 3. Hierarchy edges (broader / narrower)
        for parent_uri in acc.broaders {
            let pcid = extract_concept_id(&parent_uri);
            if !pcid.is_empty() && pcid != concept_id {
                hierarchy_edges.push(RawHierarchyEdge {
                    vocabulary: vocab.clone(),
                    concept_id: concept_id.clone(),
                    broader_concept_id: pcid,
                    relation_type: "broader".to_string(),
                });
            }
        }

        for child_uri in acc.narrowers {
            let ncid = extract_concept_id(&child_uri);
            if !ncid.is_empty() && ncid != concept_id {
                hierarchy_edges.push(RawHierarchyEdge {
                    vocabulary: vocab.clone(),
                    concept_id: ncid,
                    broader_concept_id: concept_id.clone(),
                    relation_type: "broader".to_string(),
                });
            }
        }
    }

    (concepts, mappings, hierarchy_edges)
}

fn parse_statement(
    subject_uri: String,
    tokenizer: &mut TurtleTokenizer,
    prefixes: &HashMap<String, String>,
    pref_langs: Option<&HashSet<String>>,
    alt_langs: Option<&HashSet<String>>,
    concepts_map: &mut HashMap<String, SubjectAccumulator>,
    concept_order: &mut Vec<String>,
) {
    if !concepts_map.contains_key(&subject_uri) {
        concept_order.push(subject_uri.clone());
        concepts_map.insert(subject_uri.clone(), SubjectAccumulator::default());
    }
    let acc = concepts_map.get_mut(&subject_uri).unwrap();

    // Loop over Predicate ObjectList ;
    'pred_loop: loop {
        let pred_tok = match tokenizer.next_token() {
            Some(t) => t,
            None => break,
        };

        let predicate = match pred_tok {
            Token::PrefixedOrKeyword(kw) => expand_term(kw, prefixes),
            Token::Iri(iri) => iri.to_string(),
            Token::Dot => break 'pred_loop,
            Token::Semicolon => continue 'pred_loop,
            _ => break 'pred_loop,
        };

        // Loop over Objects separated by Comma
        'obj_loop: loop {
            let obj_tok = match tokenizer.next_token() {
                Some(t) => t,
                None => break 'pred_loop,
            };

            match obj_tok {
                Token::Literal { value, lang } => {
                    let l = lang.unwrap_or("und").to_ascii_lowercase();
                    if predicate == "http://www.w3.org/2004/02/skos/core#prefLabel" {
                        if is_lang_allowed(&l, pref_langs) {
                            acc.pref_labels.insert(l, value);
                        }
                    } else if predicate == "http://www.w3.org/2004/02/skos/core#altLabel" {
                        if is_lang_allowed(&l, alt_langs) {
                            acc.alt_labels.entry(l).or_default().push(value);
                        }
                    } else if predicate == "http://www.w3.org/2002/07/owl#deprecated" {
                        let lower = value.to_ascii_lowercase();
                        if lower == "true" || lower == "1" {
                            acc.is_deprecated = true;
                        }
                    }
                }
                Token::Iri(iri) => {
                    handle_iri_object(iri, &predicate, acc);
                }
                Token::PrefixedOrKeyword(word) => {
                    let lower = word.to_ascii_lowercase();
                    if predicate == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type" {
                        let expanded = expand_term(word, prefixes);
                        if expanded == "http://www.w3.org/2004/02/skos/core#Concept" {
                            acc.is_concept = true;
                        }
                    } else if predicate == "http://www.w3.org/2002/07/owl#deprecated"
                        && (lower == "true" || lower == "1")
                    {
                        acc.is_deprecated = true;
                    } else {
                        let expanded = expand_term(word, prefixes);
                        handle_iri_object(&expanded, &predicate, acc);
                    }
                }
                Token::OpenBracket => {
                    // Blank node object, skip inside
                    let mut depth = 1;
                    while depth > 0 {
                        match tokenizer.next_token() {
                            Some(Token::OpenBracket) => depth += 1,
                            Some(Token::CloseBracket) => depth -= 1,
                            None => break,
                            _ => {}
                        }
                    }
                }
                Token::Dot => {
                    break 'pred_loop;
                }
                Token::Semicolon => {
                    break 'obj_loop;
                }
                Token::Comma => {
                    continue 'obj_loop;
                }
                _ => {}
            }

            // After object, expect ',', ';', or '.'
            match tokenizer.next_token() {
                Some(Token::Comma) => continue 'obj_loop,
                Some(Token::Semicolon) => break 'obj_loop,
                Some(Token::Dot) => break 'pred_loop,
                None => break 'pred_loop,
                _ => break 'pred_loop,
            }
        }
    }
}

fn handle_iri_object(obj_iri: &str, predicate: &str, acc: &mut SubjectAccumulator) {
    match predicate {
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type" => {
            if obj_iri == "http://www.w3.org/2004/02/skos/core#Concept" {
                acc.is_concept = true;
            }
        }
        "http://www.w3.org/2004/02/skos/core#inScheme" => {
            if acc.in_scheme.is_none() {
                acc.in_scheme = Some(obj_iri.to_string());
            }
        }
        "http://purl.org/dc/terms/isReplacedBy" | "http://purl.org/dc/elements/1.1/isReplacedBy" => {
            acc.replacements.push(obj_iri.to_string());
        }
        "http://www.w3.org/2004/02/skos/core#exactMatch" => {
            acc.exact_matches.push(obj_iri.to_string());
        }
        "http://www.w3.org/2004/02/skos/core#broader" => {
            acc.broaders.push(obj_iri.to_string());
        }
        "http://www.w3.org/2004/02/skos/core#narrower" => {
            acc.narrowers.push(obj_iri.to_string());
        }
        _ => {}
    }
}

#[pyfunction]
#[pyo3(signature = (
    content,
    default_vocab=None,
    legacy_thesauri=None,
    vocab_target_fields=None,
    default_target_field=None,
    pref_langs=None,
    alt_langs=None,
    custom_patterns=None,
))]
pub fn py_parse_turtle_content(
    content: &str,
    default_vocab: Option<&str>,
    legacy_thesauri: Option<Vec<String>>,
    vocab_target_fields: Option<HashMap<String, String>>,
    default_target_field: Option<&str>,
    pref_langs: Option<Vec<String>>,
    alt_langs: Option<Vec<String>>,
    custom_patterns: Option<HashMap<String, Vec<String>>>,
) -> (
    Vec<RawVocabConcept>,
    Vec<RawConceptMapping>,
    Vec<RawHierarchyEdge>,
) {
    let leg_set: Option<HashSet<String>> = legacy_thesauri.map(|v| v.into_iter().collect());
    let p_langs: Option<HashSet<String>> = pref_langs.map(|v| v.into_iter().collect());
    let a_langs: Option<HashSet<String>> = alt_langs.map(|v| v.into_iter().collect());

    let pats_vec: Option<Vec<(String, String)>> = custom_patterns.map(|map| {
        let mut list = Vec::new();
        for (voc, pats) in map {
            for pat in pats {
                list.push((pat.to_ascii_lowercase(), voc.to_ascii_lowercase()));
            }
        }
        list.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
        list
    });

    parse_turtle_content_internal(
        content,
        default_vocab,
        leg_set.as_ref(),
        vocab_target_fields.as_ref(),
        default_target_field,
        p_langs.as_ref(),
        a_langs.as_ref(),
        pats_vec.as_deref(),
    )
}

#[pyfunction]
#[pyo3(signature = (
    file_path,
    default_vocab=None,
    legacy_thesauri=None,
    vocab_target_fields=None,
    default_target_field=None,
    pref_langs=None,
    alt_langs=None,
    custom_patterns=None,
))]
pub fn py_parse_turtle_file(
    file_path: &str,
    default_vocab: Option<&str>,
    legacy_thesauri: Option<Vec<String>>,
    vocab_target_fields: Option<HashMap<String, String>>,
    default_target_field: Option<&str>,
    pref_langs: Option<Vec<String>>,
    alt_langs: Option<Vec<String>>,
    custom_patterns: Option<HashMap<String, Vec<String>>>,
) -> PyResult<(
    Vec<RawVocabConcept>,
    Vec<RawConceptMapping>,
    Vec<RawHierarchyEdge>,
)> {
    let p = Path::new(file_path);
    let content = fs::read_to_string(p).map_err(pyo3::exceptions::PyIOError::new_err)?;
    let inferred_vocab = default_vocab.or_else(|| p.file_stem().and_then(|s| s.to_str()));
    Ok(py_parse_turtle_content(
        &content,
        inferred_vocab,
        legacy_thesauri,
        vocab_target_fields,
        default_target_field,
        pref_langs,
        alt_langs,
        custom_patterns,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_concept_id() {
        assert_eq!(
            extract_concept_id("http://www.yso.fi/onto/yso/p040794"),
            "p040794"
        );
        assert_eq!(
            extract_concept_id("http://urn.fi/urn:nbn:fi:au:seko:00003"),
            "00003"
        );
        assert_eq!(extract_concept_id("(FI-YSO)p2591"), "p2591");
        assert_eq!(extract_concept_id("http://example.org#id123"), "id123");
    }

    #[test]
    fn test_parse_turtle_simple_snippet() {
        let ttl = r#"
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix yso: <http://www.yso.fi/onto/yso/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

yso:p1234 a skos:Concept ;
    skos:prefLabel "testikäsite"@fi, "test concept"@en ;
    skos:altLabel "synonyymi"@fi ;
    skos:broader yso:p1000 ;
    owl:deprecated false .
"#;
        let (concepts, mappings, edges) =
            parse_turtle_content_internal(ttl, Some("yso"), None, None, None, None, None, None);

        assert_eq!(concepts.len(), 1);
        let c = &concepts[0];
        assert_eq!(c.concept_id, "p1234");
        assert_eq!(c.vocabulary, "yso");
        assert_eq!(c.pref_labels.get("fi").unwrap(), "testikäsite");
        assert_eq!(c.pref_labels.get("en").unwrap(), "test concept");
        assert_eq!(c.alt_labels.get("fi").unwrap()[0], "synonyymi");
        assert_eq!(c.is_deprecated, false);

        assert_eq!(edges.len(), 1);
        assert_eq!(edges[0].concept_id, "p1234");
        assert_eq!(edges[0].broader_concept_id, "p1000");
        assert_eq!(mappings.len(), 0);
    }
}
