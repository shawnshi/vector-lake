## 2024-05-12 - LibYAML Fast Path Optimization
**Learning:** PyYAML provides C-extensions (`CSafeLoader` and `CDumper`) that dramatically outperform pure Python implementations (~5x speedup for parsing), but they may not be available on all systems if libyaml is not installed.
**Action:** Always attempt to import `CSafeLoader` and `CDumper` within a `try...except ImportError` block. Update calls to use `yaml.load(data, Loader=SafeLoader)` instead of `yaml.safe_load(data)` to enable the speedup while preserving security.
