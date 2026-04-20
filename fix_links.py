import json
import os
import re

from vector_lake.wiki_utils import get_wiki_dir, write_markdown_file

def make_node(id_str, title, type_val, content):
    fm = {
        "id": id_str,
        "title": title,
        "type": type_val,
        "domain": "Artificial_Intelligence",
        "topic_cluster": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Artificial_Intelligence"]
    }
    filepath = os.path.join(get_wiki_dir(), f"{id_str}.md")
    write_markdown_file(filepath, fm, content)
    print(f"Created {id_str}.md")

nodes = [
    ("Concept_JEPA", "JEPA (Joint Embedding Predictive Architecture)", "concept", "# JEPA (Joint Embedding Predictive Architecture)\n\nA model architecture proposed by Yann LeCun for learning abstract representations by predicting the embeddings of missing parts of inputs, aiming to build a more robust World Model without generating raw pixels."),
    ("Entity_WiNEX_Copilot", "WiNEX Copilot", "entity", "# WiNEX Copilot\n\nAn AI copilot integrated into the WiNEX medical suite to assist clinicians with documentation and decision support."),
    ("Concept_隔离沙箱", "隔离沙箱 (Isolation Sandbox)", "concept", "# 隔离沙箱 (Isolation Sandbox)\n\nA secure, isolated execution environment for untrusted or sensitive processes, particularly in federated learning or clinical AI deployments."),
    ("Concept_业务规则引擎分离", "业务规则引擎分离 (Separation of Business Rule Engine)", "concept", "# 业务规则引擎分离 (Separation of Business Rule Engine)\n\nAn architectural pattern where business rules and logic are decoupled from the core application code and managed by a dedicated engine.")
]

for node in nodes:
    make_node(*node)
