## 2024-04-26 - Optimize YAML parsing performance
**Learning:** Python's `yaml.safe_load` uses pure Python which is slow. Using `yaml.load(data, Loader=SafeLoader)` after importing `CSafeLoader as SafeLoader` with a fallback to `yaml.SafeLoader` significantly improves YAML parsing speed without risking portability in environments lacking LibYAML.
**Action:** For optimal YAML parsing performance, use `yaml.load(data, Loader=SafeLoader)` with the C-based loader fallback pattern.
