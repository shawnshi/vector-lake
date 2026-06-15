
## 2024-06-15 - Fast YAML Parsing with CSafeLoader/CSafeDumper fallback
**Learning:** Vector Lake's architecture relies heavily on Markdown files where metadata is encoded as YAML frontmatter on every single file. This means YAML parsing and dumping is a critical performance path during index generation and validation scans. The default `yaml.safe_load` and `yaml.dump` are pure Python implementations and can be slow for a large number of files.
**Action:** Always use the `CSafeLoader` and `CSafeDumper` provided by LibYAML (via `pyyaml`'s C extensions) if available, with a fallback to the pure Python implementations for compatibility. The `load_yaml` and `dump_yaml` helpers in `vector_lake.yaml_utils` should be used throughout the codebase for YAML operations.
