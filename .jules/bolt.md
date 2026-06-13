## 2024-06-13 - [Performance Improvement] Speed up YAML parsing and dumping
**Learning:** Using `yaml.safe_load` and `yaml.dump` with the default pure Python implementation is a huge performance bottleneck for processing thousands of Markdown files with YAML frontmatter.
**Action:** Added `yaml_utils.py` module to dynamically load `CSafeLoader` and `CSafeDumper` if the C extension is available, dropping parsing/dumping time by over 5x. Applied this across vector_lake and scripts.
