import json
import traceback
import sys
from datetime import datetime
from pathlib import Path
from functools import wraps

def get_meta_dir() -> Path:
    # Fallback to absolute path if wiki_utils is not available in isolated context
    return Path(r"C:\Users\shich\.gemini\MEMORY\wiki\.meta")

def log_friction(component_name: str, exception: Exception):
    """
    Distills a raw Python exception into a structured JSON log.
    """
    meta_dir = get_meta_dir()
    friction_log_path = meta_dir / "operational_friction.json"
    
    # Ensure directory exists
    meta_dir.mkdir(parents=True, exist_ok=True)
    
    # Determine error type and a brief message
    error_type = type(exception).__name__
    error_msg = str(exception)
    
    # Extract the last frame of the traceback for precise location
    tb = traceback.extract_tb(exception.__traceback__)
    if tb:
        last_frame = tb[-1]
        location = f"{last_frame.filename}:{last_frame.lineno} in {last_frame.name}"
    else:
        location = "Unknown"
        
    friction_entry = {
        "timestamp": datetime.now().isoformat(),
        "component": component_name,
        "error_type": error_type,
        "message": error_msg,
        "location": location,
        "agent_insight": f"NLAH (Never Let Agent Hallucinate): '{component_name}' encountered '{error_type}'. If this recurs, do NOT retry blindly. Rethink the approach or fallback."
    }
    
    # Read existing, append, write back
    logs = []
    if friction_log_path.exists():
        try:
            with open(friction_log_path, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except json.JSONDecodeError:
            logs = []
            
    # Keep only the last 50 frictions to avoid bloat
    logs.append(friction_entry)
    logs = logs[-50:]
    
    with open(friction_log_path, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

def capture_friction(component_name: str):
    """
    A decorator to automatically catch exceptions, distill them, and re-raise.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                log_friction(component_name, e)
                # Re-raise so the original system still knows it failed, 
                # but the friction is now recorded.
                raise
        return wrapper
    return decorator
