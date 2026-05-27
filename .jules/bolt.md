## 2024-05-27 - LibYAML C Extension Optimizations
**Learning:** This codebase uses pure Python yaml.safe_load() and yaml.dump() which is a known performance bottleneck for heavily used data structures like the YAML frontmatter. Profiling shows ~7x speedup with CSafeLoader and ~4x with CDumper.
**Action:** Use PyYAML's C extensions (CSafeLoader, CDumper) by attempting an import and falling back to Python ones if missing.
