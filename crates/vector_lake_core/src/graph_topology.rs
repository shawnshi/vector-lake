use std::collections::{HashMap, HashSet};
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[derive(Clone, Debug)]
pub struct FastNodeInput {
    pub key: String,
    pub node_type: String,
    pub links: Vec<String>,
    pub sources: Vec<String>,
    pub triples: HashMap<String, f64>,
    pub multiplier: f64,
    pub degree_weight: f64,
}

#[pyfunction]
#[pyo3(signature = (nodes_payload, type_affinity_table, overlap_weight=2.0, min_relevance=1.5, max_edges_per_node=50))]
pub fn fast_calculate_weighted_edges(
    nodes_payload: &Bound<'_, PyDict>,
    type_affinity_table: &Bound<'_, PyDict>,
    overlap_weight: Option<f64>,
    min_relevance: Option<f64>,
    max_edges_per_node: Option<usize>,
) -> PyResult<Vec<HashMap<String, PyObject>>> {
    let py = nodes_payload.py();
    let overlap_w = overlap_weight.unwrap_or(2.0);
    let min_rel = min_relevance.unwrap_or(1.5);
    let edge_cap = max_edges_per_node.unwrap_or(50);

    // 1. 解包类型亲和度表
    let mut affinity_cache: HashMap<(String, String), f64> = HashMap::new();
    for (type_a, inner_val) in type_affinity_table.iter() {
        let t_a: String = type_a.extract()?;
        if let Ok(inner_dict) = inner_val.downcast::<PyDict>() {
            for (type_b, score_val) in inner_dict.iter() {
                let t_b: String = type_b.extract()?;
                let score: f64 = score_val.extract()?;
                affinity_cache.insert((t_a.clone(), t_b), score);
            }
        }
    }

    // 2. 解包节点信息
    let mut node_keys: Vec<String> = Vec::with_capacity(nodes_payload.len());
    let mut nodes: HashMap<String, FastNodeInput> = HashMap::with_capacity(nodes_payload.len());

    let mut source_to_nodes: HashMap<String, Vec<String>> = HashMap::new();
    let mut reverse_links: HashMap<String, HashSet<String>> = HashMap::new();

    for (k, v) in nodes_payload.iter() {
        let key: String = k.extract()?;
        if let Ok(dict) = v.downcast::<PyDict>() {
            let node_type: String = dict.get_item("type")?
                .and_then(|x| x.extract::<String>().ok())
                .unwrap_or_else(|| "concept".to_string())
                .to_lowercase();

            let links: Vec<String> = dict.get_item("links")?
                .and_then(|x| x.extract::<Vec<String>>().ok())
                .unwrap_or_default();

            let sources: Vec<String> = dict.get_item("sources")?
                .and_then(|x| x.extract::<Vec<String>>().ok())
                .unwrap_or_default();

            let mut triples: HashMap<String, f64> = HashMap::new();
            if let Some(t_item) = dict.get_item("triples")? {
                if let Ok(t_dict) = t_item.downcast::<PyDict>() {
                    for (tgt, w_val) in t_dict.iter() {
                        if let (Ok(t_str), Ok(w_num)) = (tgt.extract::<String>(), w_val.extract::<f64>()) {
                            triples.insert(t_str, w_num);
                        }
                    }
                }
            }

            let multiplier: f64 = dict.get_item("multiplier")?
                .and_then(|x| x.extract::<f64>().ok())
                .unwrap_or(1.0);

            let degree_weight: f64 = dict.get_item("degree_weight")?
                .and_then(|x| x.extract::<f64>().ok())
                .unwrap_or(0.0);

            for s in &sources {
                source_to_nodes.entry(s.clone()).or_default().push(key.clone());
            }
            for l in &links {
                reverse_links.entry(l.clone()).or_default().insert(key.clone());
            }

            node_keys.push(key.clone());
            nodes.insert(
                key.clone(),
                FastNodeInput {
                    key,
                    node_type,
                    links,
                    sources,
                    triples,
                    multiplier,
                    degree_weight,
                },
            );
        }
    }

    // 3. 快速拓扑边权重计算
    let mut raw_edges: Vec<(String, String, f64)> = Vec::new();

    for key_a in &node_keys {
        let node_a = match nodes.get(key_a) {
            Some(n) => n,
            None => continue,
        };

        let mut candidate_source_overlaps: HashMap<&String, usize> = HashMap::new();
        let mut candidate_neighbor_scores: HashMap<&String, f64> = HashMap::new();

        for source in &node_a.sources {
            if let Some(b_nodes) = source_to_nodes.get(source) {
                for key_b in b_nodes {
                    if key_a < key_b {
                        *candidate_source_overlaps.entry(key_b).or_insert(0) += 1;
                    }
                }
            }
        }

        for neighbor in &node_a.links {
            if let Some(b_nodes) = reverse_links.get(neighbor) {
                let deg_w = nodes.get(neighbor).map(|n| n.degree_weight).unwrap_or(0.0);
                for key_b in b_nodes {
                    if key_a < key_b {
                        *candidate_neighbor_scores.entry(key_b).or_insert(0.0) += deg_w;
                    }
                }
            }
        }

        let mut candidates: HashSet<&String> = HashSet::new();
        for k in candidate_source_overlaps.keys() {
            candidates.insert(*k);
        }
        for k in candidate_neighbor_scores.keys() {
            candidates.insert(*k);
        }
        for l in &node_a.links {
            if key_a < l {
                candidates.insert(l);
            }
        }
        if let Some(rev) = reverse_links.get(key_a) {
            for b in rev {
                if key_a < b {
                    candidates.insert(b);
                }
            }
        }

        for key_b in candidates {
            let node_b = match nodes.get(key_b) {
                Some(n) => n,
                None => continue,
            };

            let mut score = 0.0f64;

            if let Some(&w) = node_a.triples.get(key_b) {
                score += w;
            }

            if let Some(&w) = node_b.triples.get(key_a) {
                score += w;
            }

            if let Some(&cnt) = candidate_source_overlaps.get(key_b) {
                score += (cnt as f64) * overlap_w;
            }

            if let Some(&n_score) = candidate_neighbor_scores.get(key_b) {
                score += n_score;
            }

            let affinity = affinity_cache
                .get(&(node_a.node_type.clone(), node_b.node_type.clone()))
                .copied()
                .unwrap_or(0.5);
            score += affinity;

            score *= node_a.multiplier * node_b.multiplier;
            let relevance = (score * 1000.0).round() / 1000.0;

            if relevance >= min_rel {
                raw_edges.push((key_a.clone(), key_b.clone(), relevance));
            }
        }
    }

    // 4. 去重并保留 Top-K 边 (dedupe_and_prune)
    raw_edges.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));

    let mut source_counts: HashMap<String, usize> = HashMap::new();
    let mut target_counts: HashMap<String, usize> = HashMap::new();
    let mut pruned_edges = Vec::with_capacity(raw_edges.len());

    for (src, tgt, w) in raw_edges {
        let sc = source_counts.get(&src).copied().unwrap_or(0);
        let tc = target_counts.get(&tgt).copied().unwrap_or(0);
        if sc >= edge_cap || tc >= edge_cap {
            continue;
        }
        *source_counts.entry(src.clone()).or_insert(0) += 1;
        *target_counts.entry(tgt.clone()).or_insert(0) += 1;

        let mut map = HashMap::new();
        map.insert("source".to_string(), src.into_py(py));
        map.insert("target".to_string(), tgt.into_py(py));
        map.insert("weight".to_string(), w.into_py(py));
        pruned_edges.push(map);
    }

    Ok(pruned_edges)
}
