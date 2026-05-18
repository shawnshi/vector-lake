## 2025-02-12 - Explicit LibYAML Support in PyYAML
**Learning:** `yaml.safe_load()` in PyYAML forces the use of the pure-Python loader which is extremely slow on large node/edge datasets, even if the LibYAML C-bindings (`CSafeLoader`) are installed.
**Action:** Use `yaml.load(data, Loader=SafeLoader)` after attempting to import `CSafeLoader as SafeLoader` with a fallback to `SafeLoader`. Similarly for `yaml.dump` and `CDumper`/`Dumper`. This achieves a ~4-8x speedup.
