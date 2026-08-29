import os
import sys
from pathlib import Path

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import get_wiki_dir

def merge_markdown_files(left_path, right_path):
    wiki_root = get_wiki_dir().resolve()
    left = Path(left_path).resolve()
    right = Path(right_path).resolve()
    if left.parent != wiki_root or right.parent != wiki_root:
        raise ValueError("Semantic merge inputs must be direct children of the wiki directory.")
    if left == right:
        raise ValueError("Semantic merge inputs must be different pages.")

    left_content = left.read_text(encoding="utf-8")
    right_content = right.read_text(encoding="utf-8")
    merged_content = merge_markdown_content(left_content, right_content)
    execute_mutation_batch(
        [
            {"filename": left.name, "content": merged_content},
            {"filename": right.name, "is_delete": True},
        ]
    )
    print(f"Merged {right} into {left} through the canonical mutation coordinator.")

if __name__ == "__main__":
    from vector_lake.runtime_paths import bootstrap_runtime_paths

    bootstrap_runtime_paths(caller="Semantic merge")
    if len(sys.argv) < 3:
        print("Usage: python semantic_merge.py <left_path> <right_path>")
        sys.exit(1)
        
    left_path = sys.argv[1]
    right_path = sys.argv[2]
    
    if not os.path.exists(left_path):
        print(f"Error: {left_path} does not exist.")
        sys.exit(1)
    if not os.path.exists(right_path):
        print(f"Error: {right_path} does not exist.")
        sys.exit(1)
        
    merge_markdown_files(left_path, right_path)
