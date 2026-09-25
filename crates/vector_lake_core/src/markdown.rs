use pyo3::prelude::*;
use pulldown_cmark::{Event, Parser, Tag, TagEnd};

#[pyclass]
#[derive(Clone, Debug)]
pub struct MarkdownBlock {
    #[pyo3(get)]
    pub kind: String,
    #[pyo3(get)]
    pub heading: Option<String>,
    #[pyo3(get)]
    pub raw_text: String,
    #[pyo3(get)]
    pub text: String,
}

#[pymethods]
impl MarkdownBlock {
    fn __repr__(&self) -> String {
        format!(
            "MarkdownBlock(kind={:?}, heading={:?}, text_len={})",
            self.kind, self.heading, self.text.len()
        )
    }
}

/// 清洗 claim 文本中的 Wikilink 与 Typed Link，使其变成纯文本
pub fn clean_claim_text(raw: &str, limit: usize) -> String {
    let mut out = String::with_capacity(raw.len());
    let mut chars = raw.chars().peekable();

    while let Some(c) = chars.next() {
        if c == '[' {
            // 检查是否为 typed link 或 wikilink
            let rest: String = chars.clone().collect();
            if let Some(end_idx) = rest.find(']') {
                let inner = &rest[..end_idx];
                // 如果是 [[Target|Alias]] 或 [pred:: [[Target|Alias]]]
                if inner.starts_with('[') && inner.ends_with(']') {
                    let link_content = &inner[1..inner.len() - 1];
                    let text_to_use = if let Some(pipe_idx) = link_content.rfind('|') {
                        &link_content[pipe_idx + 1..]
                    } else {
                        link_content
                    };
                    out.push_str(text_to_use);
                    for _ in 0..=end_idx {
                        chars.next();
                    }
                    continue;
                }
            }
        }
        out.push(c);
        if out.chars().count() >= limit {
            break;
        }
    }
    out
}

/// 快速分割 Frontmatter 与 Body
#[pyfunction]
pub fn fast_split_frontmatter(content: &str) -> (String, String) {
    if !content.starts_with("---\n") && !content.starts_with("---\r\n") {
        return (String::new(), content.to_string());
    }

    let search_start = 4;
    let end_marker = "\n---";
    if let Some(pos) = content[search_start..].find(end_marker) {
        let yaml_end = search_start + pos;
        let yaml_part = &content[search_start..yaml_end];

        let mut body_start = yaml_end + end_marker.len();
        if content[body_start..].starts_with("\r\n") {
            body_start += 2;
        } else if content[body_start..].starts_with('\n') {
            body_start += 1;
        }

        let body_part = &content[body_start..];
        (yaml_part.to_string(), body_part.to_string())
    } else {
        (String::new(), content.to_string())
    }
}

/// 快速统计指定章节标记下的列表项数量
#[pyfunction]
pub fn fast_count_list_items(body: &str, section_marker: &str) -> usize {
    let mut count = 0;
    let mut in_section = false;

    for line in body.lines() {
        let stripped = line.trim();
        if stripped.starts_with("## ") {
            in_section = stripped.contains(section_marker);
        } else if stripped.starts_with("# ") {
            in_section = false;
        }

        if in_section && (stripped.starts_with("- ") || stripped.starts_with("* ")) {
            count += 1;
        }
    }

    count
}

/// 使用 pulldown-cmark 提取标题、段落和列表项 blocks
#[pyfunction]
pub fn fast_extract_blocks(body: &str) -> Vec<MarkdownBlock> {
    let parser = Parser::new(body);
    let mut blocks = Vec::new();

    let mut current_heading: Option<String> = None;
    let mut active_tag: Option<&str> = None;
    let mut current_text = String::new();

    for event in parser {
        match event {
            Event::Start(Tag::Heading { .. }) => {
                active_tag = Some("heading");
                current_text.clear();
            }
            Event::End(TagEnd::Heading(_)) => {
                current_heading = Some(current_text.trim().to_string());
                current_text.clear();
                active_tag = None;
            }
            Event::Start(Tag::Paragraph) => {
                active_tag = Some("paragraph");
                current_text.clear();
            }
            Event::End(TagEnd::Paragraph) => {
                let raw = current_text.trim().to_string();
                if !raw.is_empty() {
                    let cleaned = clean_claim_text(&raw, 280);
                    blocks.push(MarkdownBlock {
                        kind: "paragraph".to_string(),
                        heading: current_heading.clone(),
                        raw_text: raw,
                        text: cleaned,
                    });
                }
                current_text.clear();
                active_tag = None;
            }
            Event::Start(Tag::Item) => {
                active_tag = Some("item");
                current_text.clear();
            }
            Event::End(TagEnd::Item) => {
                let raw = current_text.trim().to_string();
                if !raw.is_empty() {
                    let cleaned = clean_claim_text(&raw, 280);
                    blocks.push(MarkdownBlock {
                        kind: "bullet".to_string(),
                        heading: current_heading.clone(),
                        raw_text: raw,
                        text: cleaned,
                    });
                }
                current_text.clear();
                active_tag = None;
            }
            Event::Text(t) => {
                if active_tag.is_some() {
                    current_text.push_str(&t);
                }
            }
            Event::Code(c) => {
                if active_tag.is_some() {
                    current_text.push_str(&c);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if active_tag.is_some() {
                    current_text.push(' ');
                }
            }
            _ => {}
        }
    }

    blocks
}

/// 快速提取正文中的 Wikilinks: `[[Target]]` or `[[Target|Alias]]`
#[pyfunction]
pub fn fast_extract_wikilinks(content: &str) -> Vec<(String, Option<String>)> {
    let mut links = Vec::new();
    let mut chars = content.char_indices().peekable();

    while let Some((i, c)) = chars.next() {
        if c == '[' && content[i..].starts_with("[[") {
            let start = i + 2;
            if let Some(end_offset) = content[start..].find("]]") {
                let inner = &content[start..start + end_offset];
                if !inner.contains('\n') && !inner.is_empty() {
                    if let Some(pipe_pos) = inner.find('|') {
                        let target = inner[..pipe_pos].trim().to_string();
                        let alias = inner[pipe_pos + 1..].trim().to_string();
                        links.push((target, Some(alias)));
                    } else {
                        links.push((inner.trim().to_string(), None));
                    }
                }
            }
        }
    }

    links
}
