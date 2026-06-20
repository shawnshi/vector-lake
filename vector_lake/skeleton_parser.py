import os
import json
import ast
import yaml
from vector_lake.yaml_utils import load_yaml, dump_yaml

def parse_static_skeleton(filepath: str) -> str:
    """
    Deterministically parses highly structured files (Python, JSON, YAML)
    to extract their skeleton without relying on an LLM.
    """
    ext = os.path.splitext(filepath)[1].lower()
    skeleton = ""
    
    if ext not in [".json", ".py", ".yaml", ".yml"]:
        return ""
        
    try:
        if ext == ".json":
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                keys = list(data.keys())
                schema_preview = {k: type(v).__name__ for k, v in data.items()}
                skeleton = (
                    "## 确定性结构 (Static Skeleton)\n"
                    "- **Data Type**: JSON Object\n"
                    f"- **Top-level Keys**: {', '.join(keys)}\n"
                    f"- **Schema Preview**: {json.dumps(schema_preview)}\n"
                )
            elif isinstance(data, list):
                length = len(data)
                first_type = type(data[0]).__name__ if length > 0 else "N/A"
                skeleton = (
                    "## 确定性结构 (Static Skeleton)\n"
                    "- **Data Type**: JSON Array\n"
                    f"- **Length**: {length}\n"
                    f"- **Element Type**: {first_type}\n"
                )
                
        elif ext == ".py":
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            tree = ast.parse(content)
            
            classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
            imports = [node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)]
            
            skeleton = (
                "## 确定性结构 (Static Skeleton)\n"
                "- **Language**: Python\n"
                f"- **Top-level Imports**: {', '.join(imports) if imports else 'None'}\n"
                f"- **Classes Defined**: {', '.join(classes) if classes else 'None'}\n"
                f"- **Functions Defined**: {', '.join(functions) if functions else 'None'}\n"
            )
            
        elif ext in [".yaml", ".yml"]:
            with open(filepath, "r", encoding="utf-8") as f:
                data = load_yaml(f)
            if isinstance(data, dict):
                keys = list(data.keys())
                skeleton = (
                    "## 确定性结构 (Static Skeleton)\n"
                    "- **Data Type**: YAML\n"
                    f"- **Top-level Keys**: {', '.join(keys)}\n"
                )
    except Exception as e:
        skeleton = f"## 确定性结构 (Static Skeleton)\n- **Parse Error**: Could not deterministically parse ({str(e)})\n"
        
    return skeleton
