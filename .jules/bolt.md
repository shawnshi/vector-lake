## 2024-05-23 - Fast YAML parsing
**Learning:** PyYAML's default pure Python parser is heavily used in indexer and wiki_utils but is ~7x slower than the C extension version (CSafeLoader).
**Action:** Use `yaml.load(data, Loader=SafeLoader)` after attempting to import `CSafeLoader as SafeLoader` from `yaml` with a fallback to the pure Python `SafeLoader`.
