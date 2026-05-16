## 2025-02-28 - PyYAML Performance Optimization
**Learning:** `yaml.safe_load` and `yaml.dump` use pure Python implementations by default which have significant parsing overhead (e.g. 4.6s vs 0.7s in large tests). However, importing `CSafeLoader` and `CDumper` provides a massive performance boost via C extensions without sacrificing safety or correctness.
**Action:** Always attempt to import `CSafeLoader` and `CDumper` when dealing with YAML parsing/dumping for critical paths in Python, falling back to pure Python equivalents if the C extensions are unavailable.
