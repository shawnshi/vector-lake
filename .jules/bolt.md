## 2024-05-08 - Optimal YAML Parsing in Python
**Learning:** PyYAML's `safe_load` uses a pure Python parser by default which can be a bottleneck when parsing many files (like in a wiki indexer). `CSafeLoader` uses a C extension (LibYAML) which is significantly faster.
**Action:** Always try to import `CSafeLoader` and fallback to `SafeLoader`, then use `yaml.load(data, Loader=SafeLoader)` instead of `yaml.safe_load(data)` for high-performance YAML parsing that remains portable.
