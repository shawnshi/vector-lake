//! CJK segmentation through `jieba-rs`, owned by this core.
//!
//! `rjieba` is the only published Python binding for jieba-rs and it links 0.9.x; its newest
//! release predates the crate's 0.10/0.11 line.  This module makes the crate version a decision in
//! *this* repository (`Cargo.toml`) instead of a property of a wheel, and exposes rjieba's call
//! shape so the two can be diffed token-for-token:
//!
//! * `cut(text)` mirrors `rjieba.cut(text)` -- HMM enabled, which is that binding's default and
//!   therefore what every existing index was built with;
//! * `cut_joined(text)` is the pre-tokenized form the FTS projection stores.
//!
//! Nothing in the runtime selects this yet: `vector_lake/tokenizer.py` still resolves `rjieba`, and
//! switching a tokenizer invalidates every lexical index (the backend is part of the FTS cache key
//! because different backends produce different token streams).  Parity has to be measured first.

use jieba_rs::Jieba;
use pyo3::prelude::*;
use std::sync::OnceLock;

fn tokenizer() -> &'static Jieba {
    // `Jieba::new()` loads the dictionary (~several MB) and is not cheap; one process-wide
    // instance is what the Python binding does too.
    static JIEBA: OnceLock<Jieba> = OnceLock::new();
    JIEBA.get_or_init(Jieba::new)
}

/// Segment `text`, HMM enabled -- `rjieba.cut(text)` in Python.
///
/// jieba-rs 0.11 returns `Token { word, start, end, .. }` rather than the `&str` the 0.9 line
/// produced, which is one reason the pin is spelled out here: the binding's call shape and the
/// crate's return type have to be checked against each other, not assumed equal.
#[pyfunction]
pub fn cut(text: &str) -> Vec<String> {
    tokenizer()
        .cut(text, true)
        .into_iter()
        .map(|token| token.word.to_string())
        .collect()
}

/// Segment and rejoin with single spaces: what `indexer` stores as pre-tokenized text.
#[pyfunction]
pub fn cut_joined(text: &str) -> String {
    tokenizer()
        .cut(text, true)
        .into_iter()
        .map(|token| token.word)
        .collect::<Vec<_>>()
        .join(" ")
}
