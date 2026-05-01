## 2024-11-20 - [YAML parsing performance]
**Learning:** PyYAML's pure python parser is extremely slow for a large number of invocations.
**Action:** Use PyYAML's C extensions (CSafeLoader, CSafeDumper) when available for significant performance improvements.
