import json
import os
import re
import math
from collections import Counter
from pathlib import Path

def calculate_cosine_similarity(text1, text2):
    def get_tokens(text):
        tokens = Counter()
        text = str(text or "").lower()
        cjk_chars = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text)
        for char in cjk_chars:
            tokens[char] += 1
        for i in range(len(cjk_chars) - 1):
            tokens[cjk_chars[i] + cjk_chars[i+1]] += 1
        latin_words = re.findall(r"[a-z0-9]+", text)
        for word in latin_words:
            tokens[word] += 2
        return tokens

    vec1 = get_tokens(text1)
    vec2 = get_tokens(text2)
    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum([vec1[x] * vec2[x] for x in intersection])
    sum1 = sum([vec1[x]**2 for x in vec1.keys()])
    sum2 = sum([vec2[x]**2 for x in vec2.keys()])
    denominator = math.sqrt(sum1) * math.sqrt(sum2)
    if not denominator: return 0.0
    return float(numerator) / denominator

def main():
    index_path = Path("C:/Users/shich/.gemini/MEMORY/wiki/index.json")
    if not index_path.exists():
        print("Index not found.")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("nodes", {})
    filtered_nodes = []
    for key, node in nodes.items():
        if node.get("type") in ("concept", "entity"):
            filtered_nodes.append({
                "key": key,
                "title": node.get("title", ""),
                "summary": node.get("summary", ""),
                "type": node.get("type")
            })

    print(f"Comparing {len(filtered_nodes)} nodes...")
    results = []
    for i in range(len(filtered_nodes)):
        for j in range(i + 1, len(filtered_nodes)):
            node_a = filtered_nodes[i]
            node_b = filtered_nodes[j]
            
            # Combine title and summary for a richer comparison
            text_a = f"{node_a['title']} {node_a['summary']}"
            text_b = f"{node_b['title']} {node_b['summary']}"
            
            sim = calculate_cosine_similarity(text_a, text_b)
            if sim > 0.95:
                results.append({
                    "sim": sim,
                    "node_a": node_a["key"],
                    "node_b": node_b["key"],
                    "title_a": node_a["title"],
                    "title_b": node_b["title"]
                })

    results.sort(key=lambda x: x["sim"], reverse=True)
    
    print(f"\n=== High Similarity Pairs (>0.9) ===\n")
    for r in results:
        print(f"Similarity: {r['sim']:.3f} | {r['node_a']} <-> {r['node_b']} | {r['title_a']} <-> {r['title_b']}")

if __name__ == "__main__":
    main()
