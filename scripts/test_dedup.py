"""Validation: Test the new dedup functions against actual wiki data."""
import sys
import os
sys.path.insert(0, r"c:\Users\shich\.gemini\extensions\vector-lake")
os.environ["PYTHONIOENCODING"] = "utf-8"

from ingest import scan_existing_sources, canonical_source_name, _normalize_raw_ref, WIKI_DIR, MEMORY_DIR
from pathlib import Path

print("=== Test 1: canonical_source_name ===")
test_cases = [
    (r"C:\Users\shich\.gemini\MEMORY\raw\article\本地自主智能体集群架构白皮书20260404.md",
     "Source_本地自主智能体集群架构白皮书20260404.md"),
    (r"C:\Users\shich\.gemini\MEMORY\raw\article\digitalhealthobserve\2025系统裂缝中的生存算法.md",
     "Source_2025系统裂缝中的生存算法.md"),
    (r"C:\Users\shich\.gemini\MEMORY\raw\article\RAG_to_Agentic_Compilation_Full.md",
     "Source_RAG_to_Agentic_Compilation_Full.md"),
]
all_pass = True
for raw_path, expected in test_cases:
    result = canonical_source_name(raw_path)
    ok = result == expected
    if not ok: all_pass = False
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {Path(raw_path).name} -> {result}")
    if not ok:
        print(f"      Expected: {expected}")

print(f"\n=== Test 2: scan_existing_sources (WIKI_DIR={WIKI_DIR}) ===")
mapping = scan_existing_sources(WIKI_DIR)
print(f"  Found {len(mapping)} raw->source mappings")
for k, v in sorted(mapping.items()):
    print(f"    {k} -> {v}")

problem_raws = [
    "raw/article/本地自主智能体集群架构白皮书20260404.md",
    "raw/article/RAG_to_Agentic_Compilation_Full.md",
    "raw/article/digitalhealthobserve/2025系统裂缝中的生存算法.md",
]
print(f"\n=== Test 3: Dedup resolution for known problem raws ===")
for raw in problem_raws:
    normalized = _normalize_raw_ref(raw)
    existing = mapping.get(normalized)
    canonical = canonical_source_name(raw)
    if existing:
        print(f"  [DEDUP HIT] {raw}")
        print(f"      Would UPDATE: {existing}")
        print(f"      (Canonical fallback: {canonical})")
    else:
        print(f"  [NEW] {raw}")
        print(f"      Would CREATE: {canonical}")

print(f"\n=== Test 4: Simulate batch prompt entry ===")
raw_abs = r"C:\Users\shich\.gemini\MEMORY\raw\article\本地自主智能体集群架构白皮书20260404.md"
try:
    raw_ref = os.path.relpath(raw_abs, str(MEMORY_DIR)).replace("\\", "/")
except ValueError:
    raw_ref = raw_abs
normalized_ref = _normalize_raw_ref(raw_ref)
existing_name = mapping.get(normalized_ref)
canonical_name = canonical_source_name(raw_abs)
target_name = existing_name if existing_name else canonical_name
action = "UPDATE" if existing_name else "CREATE"
print(f"  raw_ref: {raw_ref}")
print(f"  normalized: {normalized_ref}")
print(f"  existing: {existing_name}")
print(f"  canonical: {canonical_name}")
print(f"  DECISION: {action} -> {target_name}")

print(f"\n=== SUMMARY ===")
print(f"  canonical_source_name tests: {'ALL PASS' if all_pass else 'SOME FAILED'}")
print(f"  scan_existing_sources found: {len(mapping)} mappings")
print(f"  All 3 problem raws have dedup hits: {all(mapping.get(_normalize_raw_ref(r)) for r in problem_raws)}")
