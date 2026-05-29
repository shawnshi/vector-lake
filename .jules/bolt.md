
## 2024-05-18 - Optimize YAML Parsing Performance
**Learning:** `yaml.safe_load()` and `yaml.dump()` use the pure Python implementations by default in PyYAML, even when libyaml C bindings are installed and available. For an application that heavily relies on YAML for markdown frontmatter parsing across thousands of files, this causes a major CPU bottleneck (taking ~10s for 10k parses vs ~1.3s with C-extensions).
**Action:** When possible, specifically import and use `CSafeLoader` and `CDumper` from `yaml` to fall back on PyYAML's C-bindings for order-of-magnitude performance boosts (8-10x), falling back to `SafeLoader` and `Dumper` if the C-extension is missing in the environment.
