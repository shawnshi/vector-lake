## 2024-05-19 - [Optimize YAML Parsing using CSafeLoader]
 **Learning:** Pure Python `yaml.safe_load` from PyYAML is significantly slower (~7x in benchmarks) than `yaml.load(..., Loader=yaml.CSafeLoader)` which uses the C-based LibYAML extension. In applications processing thousands of files (like indexing Wiki documents), this causes a noticeable bottleneck.
 **Action:** Always attempt to import `CSafeLoader as SafeLoader` with a fallback to pure Python `SafeLoader`, and use `yaml.load(..., Loader=SafeLoader)` instead of `yaml.safe_load()`.
