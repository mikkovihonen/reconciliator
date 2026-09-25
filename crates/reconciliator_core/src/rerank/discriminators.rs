use hashbrown::HashSet;
use regex::Regex;
use std::sync::OnceLock;

static DIGIT_REGEX: OnceLock<Regex> = OnceLock::new();
static VOL_PREFIX_REGEX: OnceLock<Regex> = OnceLock::new();
static VOL_SUFFIX_REGEX: OnceLock<Regex> = OnceLock::new();

fn get_digit_regex() -> &'static Regex {
    DIGIT_REGEX.get_or_init(|| Regex::new(r"\b\d+\b").unwrap())
}

fn get_vol_prefix_regex() -> &'static Regex {
    VOL_PREFIX_REGEX.get_or_init(|| {
        Regex::new(r"(?i)\b(?:osa|vol|volume|del|part|bd|band|pt|tome|vihko|häft|nide|teos)\.?\s*(\d+|[ivxlcdm]+)\b").unwrap()
    })
}

fn get_vol_suffix_regex() -> &'static Regex {
    VOL_SUFFIX_REGEX.get_or_init(|| {
        Regex::new(r"(?i)\b(\d+|[ivxlcdm]+)\.?\s*(?:osa|vol|volume|del|part|bd|band|pt|tome|vihko|häft|nide|teos)\b").unwrap()
    })
}

fn roman_to_int(s: &str) -> Option<usize> {
    let s = s.to_lowercase();
    let mut total = 0;
    let mut prev = 0;
    for c in s.chars().rev() {
        let val = match c {
            'i' => 1,
            'v' => 5,
            'x' => 10,
            'l' => 50,
            'c' => 100,
            'd' => 500,
            'm' => 1000,
            _ => return None,
        };
        if val < prev {
            total -= val;
        } else {
            total += val;
            prev = val;
        }
    }
    if total > 0 {
        Some(total)
    } else {
        None
    }
}

/// Normalize volume / part designations (e.g. "osa 2", "Vol. II", "2. osa" -> "osa 2").
pub fn normalize_volume_fast(text: &str, format_template: &str) -> Option<String> {
    if text.is_empty() {
        return None;
    }

    if let Some(caps) = get_vol_prefix_regex().captures(text) {
        if let Some(m) = caps.get(1) {
            let num_str = m.as_str().to_lowercase();
            if let Ok(num) = num_str.parse::<usize>() {
                return Some(format_template.replace("{num}", &num.to_string()));
            }
            if let Some(roman_num) = roman_to_int(&num_str) {
                return Some(format_template.replace("{num}", &roman_num.to_string()));
            }
        }
    }

    if let Some(caps) = get_vol_suffix_regex().captures(text) {
        if let Some(m) = caps.get(1) {
            let num_str = m.as_str().to_lowercase();
            if let Ok(num) = num_str.parse::<usize>() {
                return Some(format_template.replace("{num}", &num.to_string()));
            }
            if let Some(roman_num) = roman_to_int(&num_str) {
                return Some(format_template.replace("{num}", &roman_num.to_string()));
            }
        }
    }

    None
}

/// Check numeric title token compatibility.
/// Rejects conflicting numeric tokens between titles that are not publication years.
pub fn check_numeric_title_tokens_compatible(
    target_title: &str,
    candidate_title: &str,
    target_year: Option<i32>,
    candidate_year: Option<i32>,
) -> bool {
    let mut pub_years: HashSet<String> = HashSet::new();
    if let Some(y) = target_year {
        if (1000..=9999).contains(&y) {
            pub_years.insert(y.to_string());
        }
    }
    if let Some(y) = candidate_year {
        if (1000..=9999).contains(&y) {
            pub_years.insert(y.to_string());
        }
    }

    let digit_re = get_digit_regex();

    let n1: HashSet<&str> = digit_re
        .find_iter(target_title)
        .map(|m| m.as_str())
        .filter(|&tok| !pub_years.contains(tok))
        .collect();

    let n2: HashSet<&str> = digit_re
        .find_iter(candidate_title)
        .map(|m| m.as_str())
        .filter(|&tok| !pub_years.contains(tok))
        .collect();

    let diff1_has_items = n1.difference(&n2).next().is_some();
    let diff2_has_items = n2.difference(&n1).next().is_some();

    // If both have numbers unique to themselves, they conflict (e.g. "Part 1" vs "Part 2")
    !(diff1_has_items && diff2_has_items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_volume_normalization() {
        assert_eq!(
            normalize_volume_fast("osa 2", "osa {num}"),
            Some("osa 2".to_string())
        );
        assert_eq!(
            normalize_volume_fast("Vol. II", "osa {num}"),
            Some("osa 2".to_string())
        );
        assert_eq!(
            normalize_volume_fast("2. osa", "osa {num}"),
            Some("osa 2".to_string())
        );
        assert_eq!(
            normalize_volume_fast("del 1", "osa {num}"),
            Some("osa 1".to_string())
        );
    }

    #[test]
    fn test_numeric_title_tokens() {
        // Conflicting parts
        assert!(!check_numeric_title_tokens_compatible(
            "Tuntematon sotilas 1",
            "Tuntematon sotilas 2",
            None,
            None
        ));
        // Compatible publication years
        assert!(check_numeric_title_tokens_compatible(
            "Vuosikirja 2020",
            "Vuosikirja 2021",
            Some(2020),
            Some(2021)
        ));
        // Same number
        assert!(check_numeric_title_tokens_compatible(
            "Osa 1",
            "Osa 1 (uusi laitos)",
            None,
            None
        ));
    }
}
