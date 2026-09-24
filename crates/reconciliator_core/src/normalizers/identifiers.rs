/// Clean non-alphanumeric characters.
pub fn clean_alphanumeric(s: &str) -> String {
    s.chars().filter(|c| c.is_alphanumeric()).collect()
}

/// Convert a valid 10-digit ISBN into a canonical 13-digit EAN string.
pub fn isbn_10_to_13(isbn10: &str) -> Option<String> {
    let clean: String = isbn10
        .chars()
        .filter(|c| c.is_ascii_digit() || *c == 'X' || *c == 'x')
        .collect();
    let clean = clean.to_ascii_uppercase();
    if clean.len() != 10 {
        return None;
    }

    let chars: Vec<char> = clean.chars().collect();
    let mut total = 0;
    for (i, &ch) in chars.iter().enumerate() {
        let val = if ch == 'X' {
            10
        } else {
            ch.to_digit(10)? as usize
        };
        total += val * (10 - i);
    }
    if total % 11 != 0 {
        return None;
    }

    let core = format!("978{}", &clean[..9]);
    let mut total_13 = 0;
    for (idx, ch) in core.chars().enumerate() {
        let val = ch.to_digit(10)? as usize;
        total_13 += val * (if idx % 2 == 0 { 1 } else { 3 });
    }
    let check_digit = (10 - (total_13 % 10)) % 10;
    Some(format!("{}{}", core, check_digit))
}

/// Validate 13-digit ISBN checksum.
pub fn validate_isbn_13(isbn13: &str) -> bool {
    if isbn13.len() != 13 || !isbn13.chars().all(|c| c.is_ascii_digit()) {
        return false;
    }
    let mut total = 0;
    for (idx, ch) in isbn13.chars().enumerate() {
        let val = match ch.to_digit(10) {
            Some(v) => v as usize,
            None => return false,
        };
        total += val * (if idx % 2 == 0 { 1 } else { 3 });
    }
    total % 10 == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_isbn_10_to_13() {
        assert_eq!(
            isbn_10_to_13("0306406152"),
            Some("9780306406157".to_string())
        );
        assert_eq!(
            isbn_10_to_13("0-306-40615-2"),
            Some("9780306406157".to_string())
        );
        assert_eq!(isbn_10_to_13("invalid"), None);
    }

    #[test]
    fn test_validate_isbn_13() {
        assert!(validate_isbn_13("9780306406157"));
        assert!(validate_isbn_13("9789510123454"));
        assert!(!validate_isbn_13("9780306406158")); // wrong check digit
        assert!(!validate_isbn_13("short"));
    }

    #[test]
    fn test_clean_alphanumeric() {
        assert_eq!(clean_alphanumeric("978-951-0-00001-0"), "9789510000010");
        assert_eq!(clean_alphanumeric("Hello, World! 123"), "HelloWorld123");
    }
}
