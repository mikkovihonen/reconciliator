use hashbrown::{HashMap, HashSet};
use rayon::prelude::*;
use regex::Regex;
use std::sync::OnceLock;

static WORD_REGEX: OnceLock<Regex> = OnceLock::new();

fn get_word_regex() -> &'static Regex {
    WORD_REGEX.get_or_init(|| Regex::new(r"\w+").unwrap())
}

/// Tokenize text into lowercase alphanumeric tokens.
pub fn tokenize_text(text: &str) -> Vec<String> {
    if text.is_empty() {
        return Vec::new();
    }
    let lower = text.to_lowercase();
    get_word_regex()
        .find_iter(&lower)
        .map(|m| m.as_str().to_string())
        .collect()
}

/// Compute Okapi BM25 similarity between a query and a single candidate document.
/// Replicates the BM25Scorer formula with single-pair IDF=1.0 and term frequency saturation.
pub fn bm25_score_single(query_tokens: &[String], doc_tokens: &[String], k1: f64, _b: f64) -> f64 {
    if query_tokens.is_empty() || doc_tokens.is_empty() {
        return 0.0;
    }

    let mut doc_freq: HashMap<&str, usize> = HashMap::with_capacity(doc_tokens.len());
    for tok in doc_tokens {
        *doc_freq.entry(tok.as_str()).or_insert(0) += 1;
    }

    let mut max_possible = 0.0;
    for &freq in doc_freq.values() {
        let f = freq as f64;
        max_possible += (f * (k1 + 1.0)) / (f + k1);
    }

    if max_possible <= 0.0 {
        return 0.0;
    }

    let mut query_seen: HashSet<&str> = HashSet::with_capacity(query_tokens.len());
    let mut raw_score = 0.0;

    for q_tok in query_tokens {
        let tok_str = q_tok.as_str();
        if query_seen.insert(tok_str) {
            if let Some(&freq) = doc_freq.get(tok_str) {
                let f = freq as f64;
                raw_score += (f * (k1 + 1.0)) / (f + k1);
            }
        }
    }

    let norm = raw_score / max_possible;
    norm.clamp(0.0, 1.0)
}

/// Compute normalized BM25 lexical similarity for a query across candidate documents.
pub fn bm25_score_batch(query: &str, documents: &[String], k1: f64, b: f64) -> Vec<f64> {
    if query.is_empty() || documents.is_empty() {
        return vec![0.0; documents.len()];
    }

    let query_tokens = tokenize_text(query);
    if query_tokens.is_empty() {
        return vec![0.0; documents.len()];
    }

    if documents.len() > 8 {
        documents
            .par_iter()
            .map(|doc| {
                let doc_tokens = tokenize_text(doc);
                bm25_score_single(&query_tokens, &doc_tokens, k1, b)
            })
            .collect()
    } else {
        documents
            .iter()
            .map(|doc| {
                let doc_tokens = tokenize_text(doc);
                bm25_score_single(&query_tokens, &doc_tokens, k1, b)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_bm25_score_exact_match() {
        let score = bm25_score_batch("hello world", &["hello world".to_string()], 1.5, 0.75);
        assert_eq!(score[0], 1.0);
    }

    #[test]
    fn test_bm25_score_subset() {
        let score = bm25_score_batch("hello", &["hello world".to_string()], 1.5, 0.75);
        assert_eq!(score[0], 0.5);
    }

    #[test]
    fn test_bm25_score_no_match() {
        let score = bm25_score_batch("notfound", &["hello world".to_string()], 1.5, 0.75);
        assert_eq!(score[0], 0.0);
    }
}
