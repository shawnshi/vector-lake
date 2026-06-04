
## 2026-06-04 - PyYAML Parsing Bottleneck
**Learning:** Pure Python `yaml.safe_load` and `yaml.dump` calls create a significant performance bottleneck when processing large numbers of files, such as in this project's wiki indexing pipeline.
**Action:** Always attempt to import and use the C-based LibYAML wrappers (`CSafeLoader` and `CDumper`) with a fallback to the pure Python implementations (`SafeLoader` and `Dumper`). Use `yaml.load(data, Loader=SafeLoader)` instead of `yaml.safe_load(data)` to enable this optimization natively in PyYAML.
