use pyo3::prelude::*;

mod gram_index;
mod markdown;
mod graph_fusion;

#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn vector_lake_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;

    // 模块 1: 内存倒排索引加速
    m.add_function(wrap_pyfunction!(gram_index::pack_postings, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::unpack_postings, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::accumulate_postings, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::fast_accumulate_terms, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::extract_grams, m)?)?;

    // 模块 2: Markdown 极速解析
    m.add_class::<markdown::MarkdownBlock>()?;
    m.add_function(wrap_pyfunction!(markdown::fast_split_frontmatter, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::fast_count_list_items, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::fast_extract_blocks, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::fast_extract_wikilinks, m)?)?;

    // 模块 3: 图遍历与召回融合
    m.add_function(wrap_pyfunction!(graph_fusion::fast_personalized_pagerank, m)?)?;
    m.add_function(wrap_pyfunction!(graph_fusion::fast_reciprocal_rank_fusion, m)?)?;

    Ok(())
}
