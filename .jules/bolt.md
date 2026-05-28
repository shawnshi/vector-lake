## 2024-05-28 - Optimize YAML parsing with CSafeLoader
**Learning:** PyYAML's `yaml.safe_load` uses a pure Python parser by default which is very slow, especially when repeatedly parsing frontmatter during indexing or other operations that touch many markdown files.
**Action:** Use `yaml.load(data, Loader=SafeLoader)` after trying to import `CSafeLoader as SafeLoader` from `yaml`. Fallback to pure Python `SafeLoader` if C extensions are unavailable. This can give a near 10x performance improvement in YAML parsing.
