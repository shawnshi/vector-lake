## 2024-05-09 - Massive YAML Parsing Speedup
**Learning:** Pure Python `yaml.safe_load` is extremely slow when parsing thousands of small files during graph index generation. Using `CSafeLoader` (with a pure Python `SafeLoader` fallback) provides over a 7x speedup since it utilizes libyaml via a C-extension.
**Action:** When parsing YAML, especially in loops or hot paths like `indexer.py`, always attempt to import and use `CSafeLoader` to significantly cut down processing times without sacrificing safety.
