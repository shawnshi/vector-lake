## 2024-05-15 - Fast YAML parsing and dumping

**Learning:** This codebase uses `yaml.safe_load` and `yaml.dump` to process thousands of markdown files (frontmatter metadata extraction) which is incredibly slow because it uses PyYAML's pure python implementation by default. PyYAML includes a `CSafeLoader` and `CDumper` built on top of `libyaml`, which parses and dumps YAML files ~7-10x faster. Because the wiki parses every file continuously during indexing and editing operations, this has a massive cumulative impact.

**Action:** Whenever parsing or writing YAML in this project, avoid the default `yaml.safe_load` and `yaml.dump(data)`. Instead, import `CSafeLoader as SafeLoader, CDumper as Dumper` with a fallback, and use `yaml.load(data, Loader=SafeLoader)` and `yaml.dump(data, Dumper=Dumper)`.
