use pyo3::prelude::*;

/// 寻找两段切片中的最长连续公共子串 (Longest Contiguous Matching Block)
/// 返回 (a_start, b_start, length)
fn find_longest_match(
    a: &[char],
    b: &[char],
    a_low: usize,
    a_high: usize,
    b_low: usize,
    b_high: usize,
) -> (usize, usize, usize) {
    let mut best_i = a_low;
    let mut best_j = b_low;
    let mut best_size = 0;

    let b_len = b_high.saturating_sub(b_low);
    if b_len == 0 || a_high <= a_low {
        return (best_i, best_j, 0);
    }

    // 针对短字符串，简单的滑动窗口矩阵在内存中极其紧凑且速度飞快
    let mut j2len = vec![0usize; b_len + 1];

    for (idx_a, &ca) in a[a_low..a_high].iter().enumerate() {
        let i = a_low + idx_a;
        let mut prev = 0;
        for (idx_b, &cb) in b[b_low..b_high].iter().enumerate() {
            let j = b_low + idx_b;
            let current = j2len[idx_b + 1];
            if ca == cb {
                let k = prev + 1;
                j2len[idx_b + 1] = k;
                if k > best_size {
                    best_i = i + 1 - k;
                    best_j = j + 1 - k;
                    best_size = k;
                }
            } else {
                j2len[idx_b + 1] = 0;
            }
            prev = current;
        }
    }

    (best_i, best_j, best_size)
}

/// 递归统计 Ratcliff-Obershelp 算法中的所有匹配字符总数
fn count_matches(
    a: &[char],
    b: &[char],
    a_low: usize,
    a_high: usize,
    b_low: usize,
    b_high: usize,
) -> usize {
    let (i, j, k) = find_longest_match(a, b, a_low, a_high, b_low, b_high);
    if k == 0 {
        return 0;
    }

    let left = if a_low < i && b_low < j {
        count_matches(a, b, a_low, i, b_low, j)
    } else {
        0
    };

    let right = if i + k < a_high && j + k < b_high {
        count_matches(a, b, i + k, a_high, j + k, b_high)
    } else {
        0
    };

    left + k + right
}

/// 与 Python difflib.SequenceMatcher.ratio() 严格等价的高性能实现
#[pyfunction]
pub fn fast_sequence_matcher_ratio(a_str: &str, b_str: &str) -> f64 {
    let a: Vec<char> = a_str.chars().collect();
    let b: Vec<char> = b_str.chars().collect();

    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }

    let matches = count_matches(&a, &b, 0, a.len(), 0, b.len());
    (2.0 * matches as f64) / (total as f64)
}

/// 批量快速比对成对字符串的相似度比率
#[pyfunction]
pub fn fast_batch_sequence_matcher_ratios(pairs: Vec<(String, String)>) -> Vec<f64> {
    pairs
        .iter()
        .map(|(a, b)| fast_sequence_matcher_ratio(a, b))
        .collect()
}
