import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_registry_import_defers_heavy_tool_modules():
    probe = (
        "import json,sys; import vector_lake.tool_registry as registry; "
        "print(json.dumps({"
        "'projection': 'vector_lake.tool_projection' in sys.modules,"
        "'graph': 'vector_lake.tool_graph' in sys.modules,"
        "'search': 'vector_lake.tool_search' in sys.modules}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "projection": False,
        "graph": False,
        "search": False,
    }

def test_indexer_import_defers_graph_analysis_dependencies():
    probe = (
        "import json,sys; import vector_lake.indexer; "
        "print(json.dumps({name: name in sys.modules for name in "
        "('numpy', 'networkx', 'community')}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "numpy": False,
        "networkx": False,
        "community": False,
    }



def test_registry_loads_and_caches_requested_tool():
    from vector_lake import tool_registry

    assert tool_registry.doctor_vector_lake is tool_registry.doctor_vector_lake


def test_compatibility_facade_caches_resolved_exports_for_reflection():
    from vector_lake import tools

    tools.__dict__.pop("doctor_vector_lake", None)

    resolved = tools.doctor_vector_lake

    assert tools.__dict__["doctor_vector_lake"] is resolved
    assert "doctor_vector_lake" in dir(tools)

def test_registry_exposes_history_retention_lazily():
    from vector_lake import tool_registry

    assert "history_retention_maintenance" in tool_registry.__all__
    assert callable(tool_registry.history_retention_maintenance)
