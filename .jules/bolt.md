
## 2024-05-22 - Optimize YAML Parsing Performance
**Learning:** In a codebase heavily processing markdown with frontmatter (like Vector Lake's 13000+ files), `yaml.safe_load` and `yaml.dump` from PyYAML without C extension support form a severe bottleneck. The pure-Python implementation is roughly 7x slower for parsing and 2.5x slower for dumping compared to using `CSafeLoader` and `CDumper`.
**Action:** Use `CSafeLoader` and `CDumper` when reading and writing frontmatter in mass-processing paths (like the indexer and wiki utils). Always implement an `ImportError` fallback to standard `SafeLoader` / `Dumper` to prevent breaking environments missing libyaml extensions.
