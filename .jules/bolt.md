
## 2025-02-18 - [YAML Serialization Speedup]
**Learning:** PyYAML provides C bindings via LibYAML (`CSafeLoader`, `CDumper`) which are substantially faster than the pure Python equivalents (up to 7x for reading and 1.7x for writing). Since Vector Lake makes heavy use of YAML frontmatter parsing to build its index, optimizing this bottleneck has a huge impact on system performance.
**Action:** Always attempt to import and use the C bindings, wrapping them in a `try...except ImportError` fallback, to guarantee max performance while preserving portability.
