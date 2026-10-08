#!/usr/bin/env python
"""Physical synthetic adapter benchmark. Never invokes a real CLI/provider or knowledge root."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vector_lake.ingest_cli import CODEX_REQUIRED_FEATURES
from vector_lake.process_control import run_contained


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 20:
        parser.error("repeats must be between 3 and 20")
    answer = {"files_written": [{"filename": "Source_probe.md", "content": "# Synthetic probe\n"}],
              "integration": {"disposition": "standalone", "reason": "Synthetic performance fixture", "relations": []}}
    receipts = {}
    with tempfile.TemporaryDirectory(prefix="relay-bench-", dir=ROOT / "scratch") as folder:
        workspace = Path(folder)
        for backend, package_name in (("gemini", "@google/gemini-cli"), ("codex", "@openai/codex")):
            package = workspace / backend / "node_modules" / package_name
            package.mkdir(parents=True)
            record = workspace / f"{backend}-probes.log"
            entry = package / "entry.py"
            entry.write_text(
                "import json,sys\nfrom pathlib import Path\n"
                f"record=Path({str(record)!r})\nanswer={answer!r}\nfeatures={sorted(CODEX_REQUIRED_FEATURES)!r}\n"
                "argv=sys.argv[1:]\n"
                "if '--help' in argv:\n"
                " with record.open('a',encoding='utf-8') as log: log.write('help\\n')\n"
                " print('--sandbox --ephemeral --output-schema --output-last-message --output-format --extensions --policy --prompt')\n"
                "elif argv[:2]==['features','list']:\n"
                " with record.open('a',encoding='utf-8') as log: log.write('features\\n')\n"
                " print('\\n'.join(feature+' stable true' for feature in features))\n"
                "else:\n"
                " sys.stdin.read()\n"
                " if '--output-last-message' in argv: Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps(answer),encoding='utf-8')\n"
                " else: print(json.dumps({'response':json.dumps(answer)}))\n", encoding="utf-8")
            (package / "package.json").write_text(json.dumps({"name": package_name, "version": "synthetic-1", "bin": {backend: "entry.py"}}), encoding="utf-8")
            binary = workspace / backend / (backend + (".cmd" if os.name == "nt" else ""))
            binary.write_text((f'@"{sys.executable}" "{entry}" %*\n' if os.name == "nt" else f'#!/bin/sh\nexec "{sys.executable}" "{entry}" "$@"\n'), encoding="utf-8")
            if os.name != "nt": binary.chmod(0o700)
            memory = workspace / backend / "MEMORY"; (memory / "raw").mkdir(parents=True)
            source = memory / "raw" / "probe.md"; raw = b"# Synthetic public source\n"; source.write_bytes(raw)
            packet = {"prompt": "Return the synthetic contracted fixture.", "metadata": {
                "processed_data": {"filepath": str(source), "hash": hashlib.md5(raw).hexdigest(),
                                   "canonical_name": "Source_probe.md", "integration_candidates": []},
                "output_contract": "Return files_written and integration only."}}
            env = dict(os.environ); env["VECTOR_LAKE_MEMORY_DIR"] = str(memory)
            env[f"VECTOR_LAKE_RUNNER_{backend.upper()}_BIN"] = str(binary)
            env.pop("VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC", None)
            modes = {}
            for mode, enabled in (("uncached", False), ("warm_cache", True)):
                durations = []
                for index in range(args.repeats + 1):  # One unmeasured import/cache warmup per mode.
                    probes_before = len(record.read_text().splitlines()) if record.exists() else 0
                    code = f"import json,sys;from vector_lake.ingest_cli import invoke_cli;print(json.dumps(invoke_cli(json.load(sys.stdin),{backend!r},use_cache={enabled!r}),sort_keys=True))"
                    started = time.perf_counter()
                    result = run_contained([sys.executable, "-c", code], input=json.dumps(packet), env=env,
                                           cwd=str(ROOT), timeout=60)
                    elapsed = (time.perf_counter() - started) * 1000
                    if result.returncode:
                        raise RuntimeError(f"Synthetic {backend} benchmark process failed")
                    try:
                        returned = json.loads(result.stdout)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"Synthetic {backend} benchmark returned invalid JSON") from exc
                    if returned != answer:
                        raise RuntimeError(f"Synthetic {backend} benchmark changed semantic output")
                    probes_after = len(record.read_text().splitlines())
                    if index:
                        durations.append(elapsed)
                        expected = 0 if enabled else (2 if backend == "codex" else 1)
                        if probes_after - probes_before != expected:
                            raise AssertionError("Warm probe count or uncached safety oracle failed")
                ordered = sorted(durations)
                modes[mode] = {"samples_ms": durations, "median_ms": statistics.median(durations),
                               "p95_ms": ordered[-1], "help_features_probes_per_call": 0 if enabled else (2 if backend == "codex" else 1)}
            modes["median_change_percent"] = 100 * (modes["warm_cache"]["median_ms"] / modes["uncached"]["median_ms"] - 1)
            receipts[backend] = modes
    payload = {"workload": "Fresh Python adapter processes + recognized synthetic npm CLIs; identical public source and semantic JSON",
               "repeats": args.repeats, "python": sys.version, "platform": sys.platform, "results": receipts,
               "oracles": {"semantic_output_equal": True, "warm_probe_count_zero": True, "no_real_provider": True},
               "limits": "Measures synthetic local startup/probe overhead only, not provider/model latency or full canonical-to-projection latency."}
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({backend: {"uncached_ms": round(data['uncached']['median_ms'], 2), "warm_ms": round(data['warm_cache']['median_ms'], 2)} for backend, data in receipts.items()}))


if __name__ == "__main__":
    main()
