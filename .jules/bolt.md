## 2024-05-11 - Optimize YAML parsing
**Learning:** PyYAML provides C extensions `CSafeLoader` and `CDumper` which are significantly faster than pure python implementations, but they are not always available depending on the system packages installed. They must be conditionally imported.
**Action:** When parsing YAML, try importing `CSafeLoader` and `CDumper` first, then fallback to `SafeLoader` and `Dumper` if they don't exist. Apply this pattern across the codebase to maximize parsing speed.
