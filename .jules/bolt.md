## 2024-05-24 - [Optimize YAML Parsing with CSafeLoader]
**Learning:** The Python `yaml.safe_load` function is very slow. `yaml.load` combined with `CSafeLoader` offers significant performance improvements for parsing YAML files. This is important for tasks parsing many small markdown frontmatter YAMLs (like the indexing phase).
**Action:** Replace `yaml.safe_load` with `yaml.load(..., Loader=SafeLoader)` after attempting to import `CSafeLoader` from `yaml` with a fallback to `SafeLoader`.
