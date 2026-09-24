use sha2::{Digest, Sha256};

/// Generate deterministic 32-character SHA-256 hex digest prefix for blocking keys.
pub fn deterministic_key_hash(key_content: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(key_content.as_bytes());
    let result = hasher.finalize();
    let hex_str = format!("{:x}", result);
    hex_str.chars().take(32).collect()
}

/// Compute primary work blocking key: hash(author_key + '::' + title_stem).
pub fn compute_work_blocking_key(author_key: &str, title_stem: &str) -> String {
    let content = format!(
        "{}::{}",
        author_key.trim().to_lowercase(),
        title_stem.trim().to_lowercase()
    );
    deterministic_key_hash(&content)
}

/// Compute series blocking key: hash(series_title_stem + '::' + clean_series_issn).
pub fn compute_series_key(
    series_title_stem: Option<&str>,
    series_issn: Option<&str>,
) -> Option<String> {
    let s_title = series_title_stem.unwrap_or("").trim();
    let s_issn = series_issn
        .unwrap_or("")
        .trim()
        .to_lowercase()
        .replace('-', "");
    if s_title.is_empty() && s_issn.is_empty() {
        return None;
    }
    let content = format!("{}::{}", s_title, s_issn);
    Some(deterministic_key_hash(&content))
}

/// Compute host citation key for journal articles: ISSN::Year::StartPage::AuthorSurname.
pub fn compute_host_citation_key(
    issn: Option<&str>,
    year: Option<&str>,
    start_page: Option<&str>,
    author_surname: Option<&str>,
) -> Option<String> {
    let issn_val = issn?;
    let year_val = year?;
    if issn_val.trim().is_empty() || year_val.trim().is_empty() {
        return None;
    }
    let clean_issn = issn_val.trim().to_lowercase().replace('-', "");
    let clean_year = year_val.trim();
    let clean_page = start_page.unwrap_or("").trim();
    let clean_author = author_surname.unwrap_or("").trim().to_lowercase();
    Some(format!(
        "{}::{}::{}::{}",
        clean_issn, clean_year, clean_page, clean_author
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_deterministic_key_hash() {
        let hash = deterministic_key_hash("waltari::sinuhe egyptilainen");
        assert_eq!(hash.len(), 32);
        // Verify stability
        let hash2 = deterministic_key_hash("waltari::sinuhe egyptilainen");
        assert_eq!(hash, hash2);
    }

    #[test]
    fn test_compute_work_blocking_key() {
        let key = compute_work_blocking_key("Waltari", "Sinuhe egyptilainen");
        assert_eq!(key, deterministic_key_hash("waltari::sinuhe egyptilainen"));
    }

    #[test]
    fn test_compute_host_citation_key() {
        let key =
            compute_host_citation_key(Some("0028-0836"), Some("2020"), Some("105"), Some("Smith"));
        assert_eq!(key, Some("00280836::2020::105::smith".to_string()));
    }
}
