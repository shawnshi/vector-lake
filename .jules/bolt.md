## 2024-05-24 - YAML Parsing Bottleneck
**Learning:** Pure Python `yaml.safe_load` is extremely slow and acts as a major bottleneck when indexing large numbers of markdown files with frontmatter. Using `CSafeLoader` provides a ~6x speedup.
**Action:** Always attempt to import and use `CSafeLoader as SafeLoader` with a fallback to `SafeLoader` when parsing YAML in performance-critical paths like the indexer or batch processing scripts.
