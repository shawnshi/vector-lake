## 2024-05-17 - [Optimized PyYAML Operations]
**Learning:** In Vector Lake, the graph index generation and wiki frontmatter parsing were bottlenecked by PyYAML's pure Python implementations (`yaml.safe_load` and `yaml.dump`).
**Action:** Replace `yaml.safe_load` with `yaml.load(..., Loader=SafeLoader)` and `yaml.dump` with `yaml.dump(..., Dumper=Dumper)` after attempting to import the C-extensions (`CSafeLoader`, `CDumper`) with a fallback to the pure Python implementations. This yields up to a 7x speedup for parsing and 3x speedup for dumping without sacrificing security or portability.
