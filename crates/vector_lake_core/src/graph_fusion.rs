use std::collections::{HashMap, HashSet};
use pyo3::prelude::*;

/// 快速 Personalized PageRank (PPR) 随机游走
/// 严格对齐 `tool_search.py` 的算法公式
#[pyfunction]
#[pyo3(signature = (adj, seed_keys, alpha=None, steps=None))]
pub fn fast_personalized_pagerank(
    adj: HashMap<String, Vec<(String, f64)>>,
    seed_keys: Vec<String>,
    alpha: Option<f64>,
    steps: Option<usize>,
) -> Vec<(String, f64)> {
    if seed_keys.is_empty() || adj.is_empty() {
        return Vec::new();
    }

    let alpha_val = alpha.unwrap_or(0.85);
    let step_count = steps.unwrap_or(2);
    let seed_set: HashSet<String> = seed_keys.iter().cloned().collect();
    let num_seeds = seed_set.len() as f64;
    let restart_mass = (1.0 - alpha_val) / num_seeds;

    let mut ppr_scores: HashMap<String, f64> = HashMap::with_capacity(seed_set.len());
    for seed in &seed_set {
        ppr_scores.insert(seed.clone(), 1.0 / num_seeds);
    }

    for _ in 0..step_count {
        let mut next_scores: HashMap<String, f64> = HashMap::with_capacity(adj.len());
        for k in adj.keys() {
            let initial = if seed_set.contains(k) { restart_mass } else { 0.0 };
            next_scores.insert(k.clone(), initial);
        }

        let mut sorted_nodes: Vec<String> = ppr_scores.keys().cloned().collect();
        sorted_nodes.sort();

        for node in &sorted_nodes {
            let current_score = ppr_scores[node];
            if let Some(neighbors) = adj.get(node) {
                let total_weight: f64 = neighbors.iter().map(|(_, w)| *w).sum();
                if total_weight <= 0.0 {
                    continue;
                }
                for (neighbor, w) in neighbors {
                    let mass = alpha_val * current_score * (w / total_weight);
                    *next_scores.entry(neighbor.clone()).or_insert(0.0) += mass;
                }
            }
        }
        ppr_scores = next_scores;
    }

    let mut result: Vec<(String, f64)> = ppr_scores.into_iter().collect();
    result.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    result
}

/// 快速 Reciprocal Rank Fusion (RRF) 融合
#[pyfunction]
#[pyo3(signature = (ranked_lists, k=None))]
pub fn fast_reciprocal_rank_fusion(
    ranked_lists: Vec<Vec<String>>,
    k: Option<f64>,
) -> Vec<(String, f64)> {
    let rrf_k = k.unwrap_or(60.0);
    let mut scores: HashMap<String, f64> = HashMap::new();

    for list in ranked_lists {
        for (rank, key) in list.into_iter().enumerate() {
            let contribution = 1.0 / (rrf_k + rank as f64 + 1.0);
            *scores.entry(key).or_insert(0.0) += contribution;
        }
    }

    let mut out: Vec<(String, f64)> = scores.into_iter().collect();
    out.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    out
}
