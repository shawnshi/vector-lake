import sys
import os
import traceback

with open("C:/Users/shich/.gemini/tmp/mcp_ping.log", "w") as f:
    f.write("MCP Script started!\n")
    f.write(f"args: {sys.argv}\n")
    f.write(f"cwd: {os.getcwd()}\n")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vector_lake.mcp_server import mcp
    
    # Optional: configure logging here to capture FastMCP logs
    import logging
    logging.basicConfig(filename="C:/Users/shich/.gemini/tmp/mcp_debug.log", level=logging.DEBUG)
    
    mcp.run()
except Exception as e:
    with open("C:/Users/shich/.gemini/tmp/mcp_crash.log", "w") as f:
        traceback.print_exc(file=f)
