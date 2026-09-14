"""Synthetic stdlib tests; never use runtime profiles, live DBs, network or models."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

MODULE = Path(__file__).with_name("broker.py")
spec = importlib.util.spec_from_file_location("relay_broker", MODULE)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.meta = self.root / "meta"
        self.spool = self.meta / "relay-spool"
        for name in ("requests", "responses"):
            (self.spool / name).mkdir(parents=True)
        self.profile = self.root / "profiles.json"
        self.profile.write_text(json.dumps({"schema_version": 1, "profiles": {"default": {"env": {
            "VECTOR_LAKE_MEMORY_DIR": str(self.root), "VECTOR_LAKE_META_DIR": str(self.meta)}}}}))
        self.config = {"schema_version": 1, "enabled": True, "allow_model_processing_raw_text": True,
                       "runner": "host_relay", "timeout_seconds": 1200, "runner_options": {
                           "spool_dir": str(self.spool), "relay_protocol_version": b.PROTOCOL}}
        (self.meta / "auto_ingest_config.json").write_text(json.dumps(self.config))
        (self.meta / ".auto_ingest_controller_state.json").write_text('{"circuit_open_until":null}')
        self.db = self.meta / "vector_lake.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE jobs(job_id TEXT, task_type TEXT, status TEXT, lease_generation INTEGER, "
                     "lease_until TEXT, completed_at TEXT, lease_owner TEXT, payload TEXT)")
        self.job, self.attempt, self.nonce = "a" * 32, "b" * 32, "c" * 64
        self.until = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
        conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)", (self.job, "ingest", "subagent_processing",
                     1, self.until, None, "synthetic-owner", json.dumps({"attempt_id": self.attempt})))
        conn.commit()
        conn.close()
        self.stem = f"{self.attempt}.1.{self.nonce}"
        self.path = self.spool / "requests" / (self.stem + ".packet.json")
        self.document = {"protocol": b.PROTOCOL, "expected_output": b.EXPECTED, "job_id": self.job,
                         "attempt_id": self.attempt, "lease_generation": 1, "nonce": self.nonce,
                         "prompt": "SYNTHETIC ONLY", "max_input_bytes": 10000, "max_output_bytes": 10000,
                         "created_at": datetime.now(timezone.utc).isoformat()}
        self.write_packet()
        self.lock_path = self.root / "global.lock"
        self.broker = b.Broker(self.profile, "default", self.lock_path)

    def tearDown(self):
        if self.broker is not None:
            self.broker.lock.close()
        self.temp.cleanup()

    def write_packet(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def update_job(self, expression, values=()):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE jobs SET " + expression, values)
        conn.commit()
        conn.close()

    def test_publish_and_preserve_job(self):
        before = self.db.read_bytes()
        packet = self.broker.packet(self.path)
        self.assertTrue(self.broker.claim(packet))
        self.broker.publish(packet, {"stem": self.stem, "output": {"job_id": self.job, "files": []}})
        target = self.spool / "responses" / (self.stem + ".response.json")
        reply = json.loads(target.read_text())
        self.assertEqual(reply["nonce"], self.nonce)
        self.assertEqual(reply["output"]["job_id"], self.job)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertTrue(self.path.exists())
        with self.assertRaises(b.GateError):
            self.broker.publish(packet, {"stem": self.stem, "output": {"job_id": self.job}})

    def test_at_most_once_across_restart(self):
        self.assertTrue(self.broker.claim(self.broker.packet(self.path)))
        self.broker.lock.close()
        self.broker = b.Broker(self.profile, "default", self.lock_path)
        self.assertFalse(self.broker.claim(self.broker.packet(self.path)))

    def test_failed_completed_and_wrong_generation(self):
        for field, value in (("status", "failed"), ("status", "completed"), ("lease_generation", 2),
                             ("completed_at", self.until), ("lease_until", "2000-01-01T00:00:00Z"),
                             ("payload", '{"attempt_id":"wrong"}')):
            with self.subTest(field=field, value=value):
                self.update_job("status='subagent_processing', lease_generation=1, completed_at=NULL, "
                                "lease_until=?, payload=?", (self.until, json.dumps({"attempt_id": self.attempt})))
                self.update_job(field + "=?", (value,))
                with self.assertRaises(b.GateError):
                    self.broker.packet(self.path)

    def test_changed_packet_or_lease_rejected_before_publish(self):
        packet = self.broker.packet(self.path)
        self.document["prompt"] = "CHANGED"
        self.write_packet()
        with self.assertRaises(b.GateError):
            self.broker.publish(packet, {"stem": self.stem, "output": {"job_id": self.job}})
        packet = self.broker.packet(self.path)
        self.update_job("status='failed'")
        with self.assertRaises(b.GateError):
            self.broker.publish(packet, {"stem": self.stem, "output": {"job_id": self.job}})

    def test_bindings_and_byte_limits(self):
        for field, value in (("job_id", "wrong"), ("nonce", "d" * 64), ("lease_generation", True),
                             ("max_input_bytes", 1), ("protocol", "wrong"),
                             ("created_at", "2000-01-01T00:00:00Z")):
            old = self.document[field]
            self.document[field] = value
            self.write_packet()
            with self.subTest(field=field), self.assertRaises(b.GateError):
                self.broker.packet(self.path)
            self.document[field] = old
        self.write_packet()
        packet = self.broker.packet(self.path)
        for result in ({"stem": "wrong", "output": {}}, {"stem": self.stem, "output": {"job_id": "wrong"}},
                       {"stem": self.stem, "output": {"job_id": self.job, "text": "x" * 20000}}):
            with self.assertRaises(b.GateError):
                self.broker.publish(packet, result)

    def test_small_output_limit_rejected_before_claim_or_ipc(self):
        for limit in (1, 512, 513, 4095):
            self.document["max_output_bytes"] = limit
            self.write_packet()
            with self.assertRaisesRegex(b.GateError, "relay_packet_output_limit"):
                self.broker.packet(self.path)
            self.assertEqual(list(self.broker.claims.iterdir()), [])

    def test_revoked_consent_and_circuit(self):
        self.config["enabled"] = False
        (self.meta / "auto_ingest_config.json").write_text(json.dumps(self.config))
        with self.assertRaises(b.GateError):
            self.broker.packet(self.path)
        self.config["enabled"] = True
        (self.meta / "auto_ingest_config.json").write_text(json.dumps(self.config))
        (self.meta / ".auto_ingest_controller_state.json").write_text(json.dumps({"circuit_open_until": self.until}))
        with self.assertRaises(b.GateError):
            self.broker.packet(self.path)

    def test_hardlink_and_directory_replacement(self):
        link = self.root / "hardlink"
        os.link(self.path, link)
        with self.assertRaises(b.GateError):
            self.broker.packet(self.path)
        link.unlink()
        responses = self.spool / "responses"
        responses.rename(self.spool / "old-responses")
        responses.mkdir()
        with self.assertRaises(b.GateError):
            self.broker.packet(self.path)

    def test_same_bytes_replaced_file_and_expired_packet(self):
        packet = self.broker.packet(self.path)
        replacement = self.root / "replacement"
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement, self.path)
        with self.assertRaises(b.GateError):
            self.broker.still_valid(packet)
        packet = self.broker.packet(self.path)
        packet["_deadline"] = time.monotonic() - 1
        with self.assertRaises(b.GateError):
            self.broker.still_valid(packet)

    def test_physical_junction_or_symlink(self):
        alias = self.root / "alias"
        if os.name == "nt":
            result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(alias),
                                     str(self.spool / "requests")], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
        else:
            alias.symlink_to(self.spool / "requests", target_is_directory=True)
        with self.assertRaises(b.GateError):
            b.Pins().add(alias)

    def test_wal_lease_change_visible_read_only(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            packet = self.broker.packet(self.path)
            conn.execute("UPDATE jobs SET status='failed'")
            conn.commit()
            with self.assertRaises(b.GateError):
                self.broker.still_valid(packet)
        finally:
            conn.close()

    def test_global_lock_other_process(self):
        result = subprocess.run([sys.executable, "-I", str(MODULE), "--lock-only", "--lock", str(self.lock_path)],
                                input=b"", capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"SYNTHETIC", result.stdout + result.stderr)

    def test_failure_and_no_overwrite_opposite_envelope(self):
        packet = self.broker.packet(self.path)
        self.broker.publish(packet, {"stem": self.stem, "failed": "relay_model_error"})
        with self.assertRaises(b.GateError):
            self.broker.publish(packet, {"stem": self.stem, "output": {"job_id": self.job}})
        self.assertFalse((self.spool / "responses" / (self.stem + ".response.json")).exists())

    def test_ipc_synthetic_roundtrip_and_eof(self):
        self.broker.lock.close()
        self.broker = None
        proc = subprocess.Popen([sys.executable, "-I", "-u", str(MODULE), "--runtime-profile", str(self.profile),
                                 "--lock", str(self.lock_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        lines = queue.Queue()
        def pump():
            for line in proc.stdout:
                lines.put(json.loads(line))
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            self.assertEqual(lines.get(timeout=5)["code"], "relay_ready")
            request = lines.get(timeout=5)
            self.assertEqual(request["type"], "generate")
            self.assertEqual(request["prompt"], "SYNTHETIC ONLY")
            proc.stdin.write((json.dumps({"stem": request["stem"], "output": {"job_id": self.job}}) + "\n").encode())
            proc.stdin.flush()
            self.assertEqual(lines.get(timeout=5)["code"], "relay_published")
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(proc.stderr.read(), b"")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
