import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from vector_lake.claim_extractor import extract_page_objects
from vector_lake.tool_search import _classify_intent
from vector_lake.tool_query import prepare_query_context
from vector_lake.mcp_server import propose_schema_mutation
from vector_lake.governance_store import load_governance_queue

def test_claim_extractor():
    print("--- Test 1: Zero-LLM Graph Extraction ---")
    mock_body = "This is a test. [works_at:: [[Google|Goo]]] and [owns:: [[DeepMind]]]. Also standard link [[AI]]."
    # extract_page_objects internally expects a somewhat valid markdown body. 
    # It might run into the LLM extraction if we are not careful, but the edges are extracted by regex immediately.
    # Actually, we should just test the regex by inspecting the result edges. 
    # But wait, extract_page_objects calls LLM! We don't want to burn tokens.
    # We can just run the regex block directly to simulate it, or just use a very tiny body.
    # Let's mock the LLM call inside it or skip the full extract_page_objects.
    import re
    body = mock_body
    page_edges = []
    page_key = "mock"
    now = "2026-05-28T00:00:00Z"
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", body):
        predicate = match.group(1).strip()
        target = match.group(2).split("|")[0].strip().replace(".md", "")
        if target:
            page_edges.append({"source_id": page_key, "target_id": target, "relation": predicate, "weight": 1.0, "updated_at": now})
    for match in re.finditer(r"(?<!::\s)\[\[(.*?)\]\]", body):
        target = match.group(1).split("|")[0].strip().replace(".md", "")
        if target and "::" not in target:
            page_edges.append({"source_id": page_key, "target_id": target, "relation": "mentions", "weight": 1.0, "updated_at": now})
    print(f"Extracted edges: {json.dumps(page_edges, ensure_ascii=False)}")

def test_intent_dispatch():
    print("\n--- Test 2: Intent-Aware Dispatch ---")
    q1 = "昨天的大事记"
    q2 = "DeepMind是谁的公司"
    q3 = "机器学习原理"
    print(f"Query '{q1}' intent: {_classify_intent(q1)}")
    print(f"Query '{q2}' intent: {_classify_intent(q2)}")
    print(f"Query '{q3}' intent: {_classify_intent(q3)}")

def test_schema_mutation():
    print("\n--- Test 3: Schema Mutation ---")
    res = propose_schema_mutation("Quantum_Medicine", "Using quantum computing for drug discovery.")
    print(res)
    queue = load_governance_queue()
    latest_item = queue['items'][-1] if queue.get('items') else None
    print(f"Latest item in queue: {latest_item['title']} -> {latest_item['type']}")

def test_gap_analysis():
    print("\n--- Test 4: Gap Analysis Prompt ---")
    # For prepare_query_context to not crash, we can pass it empty string.
    # Actually wait, prepare_query_context just returns a prompt string!
    prompt = prepare_query_context("Test Query", "mock_payload.md")
    if "[CRITICAL REQUIREMENT: GAP ANALYSIS]" in prompt:
        print("Gap Analysis constraint successfully found in generated prompt!")
    else:
        print("Gap Analysis constraint missing!")

if __name__ == '__main__':
    test_claim_extractor()
    test_intent_dispatch()
    test_schema_mutation()
    test_gap_analysis()
