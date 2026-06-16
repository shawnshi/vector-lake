## 2026-06-16 - YAML parsing bottleneck in indexer
**Learning:** Vector Lake's architecture stores all knowledge graph node data in markdown file frontmatter, meaning the indexer must parse hundreds or thousands of YAML blocks during generation. The default pure-python `yaml.safe_load` is extremely slow.
**Action:** Created `vector_lake.yaml_utils` to transparently fallback to the `CSafeLoader` C-extension implementation provided by LibYAML, reducing full indexer generation time on 1000 items from ~6.6s to ~3.6s (a 45% speedup). Update all code reading/writing frontmatter to use these utils.
