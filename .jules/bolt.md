## 2025-02-18 - Optimize YAML Parsing Performance
**Learning:** Parsing YAML frontmatter with pure Python `yaml.safe_load` is a major performance bottleneck, especially when indexing or processing a large number of Markdown files. Using `CSafeLoader` (via LibYAML C extension) when available provides an 8x speedup.
**Action:** Always attempt to import `CSafeLoader as SafeLoader` from `yaml`, and fallback to pure Python `SafeLoader`. Then, use `yaml.load(data, Loader=SafeLoader)` instead of `yaml.safe_load(data)` for high-performance parsing without sacrificing safety or portability.
