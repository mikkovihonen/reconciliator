use regex::Regex;
use std::sync::OnceLock;
use unicode_normalization::char::is_combining_mark;
use unicode_normalization::UnicodeNormalization;

pub const DEFAULT_PRESERVED_DIACRITICS: &str = "åäöÅÄÖ";

static BIOGRAPHICAL_DATE_RE: OnceLock<Regex> = OnceLock::new();

fn get_biographical_date_regex() -> &'static Regex {
    BIOGRAPHICAL_DATE_RE.get_or_init(|| {
        let pattern = r"(?i)(?:\s*\(\s*(?:s\.|synt\.|d\.|k\.|kuol\.|b\.|c\.|ca\.|n\.|noin\.|fl\.)?\s*\b(?:\d{1,4}\s*[-–]\s*\d{0,4}|\d{1,4}|\d{1,2}(?:th|st|nd|rd)\s+cent(?:\.|ury)?|\d{3,4}-(?:luku|talet|l\.|t\.))\s*\)\s*$|,\s*(?:s\.|synt\.|d\.|k\.|kuol\.|b\.|c\.|ca\.|n\.|noin\.|fl\.)?\s*\b(?:\d{1,4}\s*[-–]\s*\d{0,4}|\d{1,4}|\d{1,2}(?:th|st|nd|rd)\s+cent(?:\.|ury)?|\d{3,4}-(?:luku|talet|l\.|t\.))\s*\.?$|,\s*[-–]\s*\d{1,4}\s*\.?$)";
        Regex::new(pattern).expect("Valid biographical date regex")
    })
}

/// Selectively fold Latin diacritics; preserve specified diacritics (default: Finnish / Swedish åäöÅÄÖ).
pub fn fold_diacritics(s: &str, preserved_diacritics: Option<&str>) -> String {
    let preserved = preserved_diacritics.unwrap_or(DEFAULT_PRESERVED_DIACRITICS);
    let mut out = String::with_capacity(s.len());

    for ch in s.nfc() {
        if ch.is_ascii() || preserved.contains(ch) {
            out.push(ch);
        } else {
            // NFKD decompose this character and drop combining marks
            for decomp in std::iter::once(ch).nfkd() {
                if !is_combining_mark(decomp) {
                    out.push(decomp);
                }
            }
        }
    }
    out
}

/// Strip trailing parenthetical or comma-separated biographical dates.
pub fn strip_biographical_dates(s: &str) -> String {
    let re = get_biographical_date_regex();
    let mut cleaned = s.to_string();
    loop {
        let new_cleaned = re.replace(&cleaned, "").trim_end().to_string();
        if new_cleaned == cleaned {
            break;
        }
        cleaned = new_cleaned;
    }
    cleaned
        .trim_end_matches(|c: char| {
            c == ',' || c == ':' || c == ';' || c == '/' || c.is_whitespace()
        })
        .to_string()
}

/// Canonical label fold for normalised lookup.
pub fn fold_label(s: &str, preserved_diacritics: Option<&str>) -> String {
    let stripped = strip_biographical_dates(s);
    let nfkc: String = stripped.nfkc().collect();
    let folded = fold_diacritics(&nfkc, preserved_diacritics).to_lowercase();
    let clean = folded.split_whitespace().collect::<Vec<_>>().join(" ");
    clean
        .trim_end_matches(['.', ',', ':', ';', '/'])
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fold_diacritics_preserves_nordic() {
        assert_eq!(fold_diacritics("Åland", None), "Åland");
        assert_eq!(
            fold_diacritics("Helsingfors och Åbo", None),
            "Helsingfors och Åbo"
        );
        assert_eq!(fold_diacritics("Äänekoski", None), "Äänekoski");
        assert_eq!(fold_diacritics("Örebro", None), "Örebro");
        assert_eq!(fold_diacritics("Hämeenlinna", None), "Hämeenlinna");
    }

    #[test]
    fn test_fold_diacritics_folds_foreign() {
        assert_eq!(fold_diacritics("München", None), "Munchen");
        assert_eq!(fold_diacritics("Curaçao", None), "Curacao");
        assert_eq!(
            fold_diacritics("Saint-Barthélemy", None),
            "Saint-Barthelemy"
        );
        assert_eq!(fold_diacritics("São Paulo", None), "Sao Paulo");
        assert_eq!(
            fold_diacritics("Göran & François", None),
            "Göran & Francois"
        );
        assert_eq!(fold_diacritics("Müller", None), "Muller");
        assert_eq!(fold_diacritics("Café", None), "Cafe");
    }

    #[test]
    fn test_custom_preserved_diacritics() {
        assert_eq!(fold_diacritics("Müller", Some("üÜ")), "Müller");
        assert_eq!(fold_diacritics("Hämeenlinna", Some("üÜ")), "Hameenlinna");
    }

    #[test]
    fn test_strip_biographical_dates() {
        assert_eq!(
            strip_biographical_dates("Waltari, Mika, 1908-1979"),
            "Waltari, Mika"
        );
        assert_eq!(
            strip_biographical_dates("Fleming, Ian (1908-1964)"),
            "Fleming, Ian"
        );
        assert_eq!(
            strip_biographical_dates("Bond, James (fiktiivinen hahmo)"),
            "Bond, James (fiktiivinen hahmo)"
        );
    }

    #[test]
    fn test_fold_label() {
        assert_eq!(
            fold_label("Waltari, Mika, 1908-1979", None),
            "waltari, mika"
        );
        assert_eq!(fold_label("Müller (1900-1975)", Some("üÜ")), "müller");
    }
}
