import sys
import os
import shutil
import subprocess
from pathlib import Path
import yaml
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("semantic-merge")

def get_gemini_exec():
    gemini_exec = shutil.which("gemini")
    if not gemini_exec:
        gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
    return gemini_exec

def split_frontmatter(content):
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
                return fm if isinstance(fm, dict) else {}, parts[2].strip()
            except yaml.YAMLError:
                pass
    return {}, content

def write_markdown_file(path, frontmatter, body):
    yaml_block = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
    # Write using standard python to avoid pulling vector_lake.wiki_utils which might be tricky if PYTHONPATH is not set
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"---\n{yaml_block}---\n{body.lstrip()}")

def semantic_merge(primary_path, secondary_path):
    primary_path = Path(primary_path)
    secondary_path = Path(secondary_path)

    if not primary_path.exists():
        log.error(f"Primary file does not exist: {primary_path}")
        return False
    if not secondary_path.exists():
        log.error(f"Secondary file does not exist: {secondary_path}")
        return False

    log.info(f"Reading [Primary] {primary_path.name} and [Secondary] {secondary_path.name}...")
    with open(primary_path, "r", encoding="utf-8") as f:
        p_content = f.read()
    with open(secondary_path, "r", encoding="utf-8") as f:
        s_content = f.read()

    fm_p, body_p = split_frontmatter(p_content)
    fm_s, body_s = split_frontmatter(s_content)

    prompt = f"""You are an expert academic and strategic Synthesizer Agent for the Vector Lake knowledge graph.
Your task is to merge two highly similar or overlapping Markdown wiki nodes into a single, highly coherent, MECE document.
    You MUST output ONLY valid markdown content (NO markdown code block backticks around the entire output).

### RULES:
1. OVERWRITE "Compiled Truth": Synthesize the core concepts, definitions, attributes, and critical relationships from both documents into a single, high-density, authoritative `## Compiled Truth (编译事实)` section at the top. Eliminate redundant synonyms. Do not invent facts.
2. APPEND "Timeline": Move all secondary details, raw source quotes, historical nuances, or fragmented notes into the `## Timeline (证据时间线)` section at the bottom. Do not throw away raw evidence, just demote it.
3. PRESERVE LINKS: You MUST preserve all `[[Wiki_Links]]` present in both documents. If both documents link to the same entity, only link it once in the synthesized truth.
4. Output ONLY the raw markdown string (excluding frontmatter). Do NOT include the YAML frontmatter. I will handle that.

--- PRIMARY FILE BODY ---
{body_p}

--- SECONDARY FILE BODY ---
{body_s}

--- EXPECTED FORMAT ---
## Compiled Truth (编译事实)
(High density synthesis of facts, concepts, and relationships)
...

---
## Timeline (证据时间线)
(Append-only log of raw evidence, old notes, and fragmented citations)
..."""
    
    log.info(f"Sending semantic merge request to LLM (gemini-3.1-pro-preview)...")
    gemini_exec = get_gemini_exec()
    cmd = [gemini_exec, "-m", "gemini-3.1-pro-preview", "-p", "You are a semantic merge agent.", "--approval-mode", "yolo"]
    
    try:
        result = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=180)
        if result.returncode != 0:
            stderr_str = result.stderr.decode('utf-8', errors='replace').strip()
            log.error(f"LLM Error (Exit Code {result.returncode}): {stderr_str}")
            return False
            
        merged_body = result.stdout.decode("utf-8", errors="replace").strip()
        if not merged_body:
            log.error("LLM returned empty output.")
            return False
            
        if merged_body.startswith("```markdown"):
            merged_body = merged_body[11:]
            if merged_body.endswith("```"):
                merged_body = merged_body[:-3]
        merged_body = merged_body.strip()
        
        # Merge aliases
        p_aliases = fm_p.get("aliases", [])
        if isinstance(p_aliases, str): p_aliases = [p_aliases]
        
        s_key = secondary_path.stem
        if s_key not in p_aliases: p_aliases.append(s_key)
        
        s_aliases = fm_s.get("aliases", [])
        if isinstance(s_aliases, str): s_aliases = [s_aliases]
        for a in s_aliases:
            if a not in p_aliases: p_aliases.append(a)
            
        fm_p["aliases"] = p_aliases
        
        # Write back
        log.info(f"Writing synthesized content to {primary_path.name}...")
        write_markdown_file(primary_path, fm_p, merged_body)
        
        # Delete secondary
        log.info(f"Deleting demoted secondary file {secondary_path.name}...")
        os.remove(secondary_path)
        
        log.info(f"Semantic merge completed successfully.")
        return True

    except subprocess.TimeoutExpired:
        log.error("LLM request timed out after 180 seconds.")
        return False
    except Exception as e:
        log.error(f"Exception during LLM call: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python semantic_merge.py <primary.md> <secondary.md>")
        sys.exit(1)
        
    p_path = sys.argv[1]
    s_path = sys.argv[2]
    
    success = semantic_merge(p_path, s_path)
    sys.exit(0 if success else 1)
