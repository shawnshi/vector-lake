## 2025-05-02 - Optimize YAML Parsing Performance
**Learning:** PyYAML's default `yaml.safe_load` uses the pure Python parser, which is extremely slow, especially when parsing thousands of markdown files with frontmatter during indexing.
**Action:** Always attempt to import `CSafeLoader` and use `yaml.load(data, Loader=SafeLoader)` to achieve order-of-magnitude faster parsing speeds while maintaining safety. Include a fallback to pure Python `SafeLoader` to maintain portability in environments where the C extension isn't available.
