## 2025-05-19 - Optimize PyYAML parsing using C-extensions

**Learning:** PyYAML's pure python implementation (`yaml.safe_load`, `yaml.dump`) is extremely slow, especially when parsing large or numerous files like those found in `MEMORY/wiki` for a knowledge graph. Using the C-extensions (`CSafeLoader`, `CDumper`) offers a massive performance boost (measured around ~8.5x faster for parsing and ~4.5x faster for dumping).

**Action:** Whenever parsing or dumping YAML repeatedly (especially in indexers, sync scripts, or utility functions), attempt to import and use `CSafeLoader`/`CDumper` with a fallback to the pure Python `SafeLoader`/`Dumper`. Avoid `yaml.safe_load(data)` directly in favor of `yaml.load(data, Loader=SafeLoader)` to ensure the C-extension is actually used if available.
