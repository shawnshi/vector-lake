
## 2024-06-25 - Optimize YAML parsing and dumping performance
**Learning:** Vector Lake's architecture relies heavily on Markdown files where metadata is encoded as YAML frontmatter on every single file, making YAML parsing and dumping a critical performance path during index generation and validation scans. The pure Python `yaml.safe_load` and `yaml.dump` implementations can be a bottleneck.
**Action:** Created `vector_lake/yaml_utils.py` to wrap YAML operations with a performance fallback between fast LibYAML C extensions (`CSafeLoader`, `CSafeDumper`) and pure Python implementations, ensuring significant speedups without risking portability issues in environments lacking LibYAML. Updated all `yaml.safe_load` and `yaml.dump` calls across the codebase to use these wrappers.
