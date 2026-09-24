use hashbrown::HashSet;

/// Compute length of longest common subsequence between two sequences of strings.
pub fn lcs_length(seq1: &[String], seq2: &[String]) -> usize {
    let m = seq1.len();
    let n = seq2.len();
    if m == 0 || n == 0 {
        return 0;
    }

    // Ensure seq2 is the shorter sequence to minimize DP array allocation
    let (s1, s2, m_len, n_len) = if m >= n {
        (seq1, seq2, m, n)
    } else {
        (seq2, seq1, n, m)
    };

    let mut dp = vec![0usize; n_len + 1];

    for i in 1..=m_len {
        let mut prev = 0;
        for j in 1..=n_len {
            let temp = dp[j];
            if s1[i - 1] == s2[j - 1] {
                dp[j] = prev + 1;
            } else {
                dp[j] = dp[j].max(dp[j - 1]);
            }
            prev = temp;
        }
    }
    dp[n_len]
}

/// Compute Jaccard similarity score between two collections of items.
pub fn jaccard_similarity(c1: &[String], c2: &[String]) -> f64 {
    if c1.is_empty() && c2.is_empty() {
        return 1.0;
    }
    if c1.is_empty() || c2.is_empty() {
        return 0.0;
    }

    let set1: HashSet<&str> = c1.iter().map(|s| s.as_str()).collect();
    let set2: HashSet<&str> = c2.iter().map(|s| s.as_str()).collect();

    let intersection_count = set1.intersection(&set2).count();
    let union_count = set1.union(&set2).count();

    if union_count == 0 {
        0.0
    } else {
        intersection_count as f64 / union_count as f64
    }
}

/// High-speed aggregate constituent overlap evaluation combining Jaccard & LCS sequence alignment.
#[allow(clippy::too_many_arguments)]
pub fn evaluate_aggregate_overlap(
    c1: &[String],
    c2: &[String],
    is_component1: bool,
    is_component2: bool,
    min_constituents: usize,
    jaccard_veto_threshold: f64,
    jaccard_merge_threshold: f64,
    sequence_alignment: bool,
    lcs_threshold: f64,
    merge_bonus_score: f64,
) -> Option<(f64, &'static str)> {
    let len1 = c1.len();
    let len2 = c2.len();

    // Container vs constituent part anti-collision guard
    if (len1 >= min_constituents && is_component2) || (len2 >= min_constituents && is_component1) {
        return Some((0.0, "container_part_collision"));
    }

    if len1 >= min_constituents && len2 >= min_constituents {
        let jaccard = jaccard_similarity(c1, c2);

        if jaccard <= jaccard_veto_threshold {
            return Some((0.0, "aggregate_content_mismatch"));
        }

        if jaccard >= jaccard_merge_threshold {
            if sequence_alignment {
                let lcs_len = lcs_length(c1, c2);
                let total_len = len1 + len2;
                let lcs_ratio = if total_len > 0 {
                    (2.0 * lcs_len as f64) / (total_len as f64)
                } else {
                    0.0
                };

                if lcs_ratio >= lcs_threshold {
                    return Some((merge_bonus_score, "aggregate_jaccard_lcs_merge"));
                }
            } else {
                return Some((merge_bonus_score, "aggregate_jaccard_merge"));
            }
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lcs_length() {
        assert_eq!(lcs_length(&[], &[]), 0);
        assert_eq!(
            lcs_length(
                &["a".into(), "b".into(), "c".into()],
                &["a".into(), "b".into(), "c".into()]
            ),
            3
        );
        assert_eq!(
            lcs_length(
                &["a".into(), "b".into(), "c".into()],
                &["x".into(), "y".into(), "z".into()]
            ),
            0
        );
        assert_eq!(
            lcs_length(
                &["a".into(), "b".into(), "c".into(), "d".into()],
                &["a".into(), "c".into(), "d".into()]
            ),
            3
        );
        assert_eq!(
            lcs_length(
                &["a".into(), "b".into(), "c".into(), "d".into()],
                &["d".into(), "c".into(), "b".into(), "a".into()]
            ),
            1
        );
    }

    #[test]
    fn test_jaccard() {
        let c1 = vec!["t1".into(), "t2".into(), "t3".into()];
        let c2 = vec!["t1".into(), "t2".into(), "t4".into()];
        // 2 in common / 4 total = 0.5
        assert!((jaccard_similarity(&c1, &c2) - 0.5).abs() < 1e-6);
    }
}
