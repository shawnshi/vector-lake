## 2024-04-22 - Replace safe_load with CSafeLoader
**Learning:** PyYAML's default safe_load is surprisingly slow because it uses pure Python. In a repository heavily reliant on parsing frontmatter across hundreds of files, this creates a measurable bottleneck.
**Action:** Always prefer importing and using CSafeLoader from the pyyaml library (falling back to SafeLoader if unavailable) instead of using yaml.safe_load()
