"""Real producer/consumer wire interoperability using only synthetic local state."""
import importlib.util
import queue
import threading
import time
from pathlib import Path

import pytest

from vector_lake.auto_ingest_runners.base import GenerationRequest
from vector_lake.auto_ingest_runners.host_relay import HOST_RELAY_ADAPTER, RELAY_PROTOCOL_VERSION
from vector_lake.auto_ingest_worker import AutoIngestPolicyError


@pytest.mark.parametrize("failed", [False, True])
def test_real_producer_accepts_consumer_envelope(failed):
    module_path = Path(__file__).parents[1] / "extensions/host-relay-consumer/test_broker.py"
    spec = importlib.util.spec_from_file_location("synthetic_broker_fixture", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.BrokerTests()
    fixture.setUp()
    thread = None
    stop = threading.Event()
    errors = queue.Queue()
    envelopes = queue.Queue()
    try:
        fixture.path.unlink()  # Remove only the fixture-owned synthetic packet.
        options = HOST_RELAY_ADAPTER.validate_options({
            "spool_dir": str(fixture.spool.resolve()),
            "relay_protocol_version": RELAY_PROTOCOL_VERSION,
            "poll_seconds": 0.05,
        })
        handle = HOST_RELAY_ADAPTER.probe(options)
        request = GenerationRequest(fixture.job, ("synthetic-owner", "synthetic-token", 1),
                                    fixture.attempt, "SYNTHETIC ONLY", 10000, 10000, 3)

        def respond():
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not stop.is_set():
                    paths = list((fixture.spool / "requests").glob("*.packet.json"))
                    if paths:
                        packet = fixture.broker.packet(paths[0])
                        assert fixture.broker.claim(packet)
                        result = {"stem": packet["_stem"]}
                        if failed:
                            result["failed"] = "relay_synthetic_failure"
                        else:
                            result["output"] = {"job_id": fixture.job, "files_written": [], "processed_data": []}
                        # Capture actual serialized bytes before the producer may clean them up.
                        fixture.broker.publish(packet, result)
                        return
                    stop.wait(0.01)
                raise AssertionError("synthetic_packet_not_published")
            except BaseException as error:
                errors.put(error)

        # Observe parsed documents through the real producer reader, not a copied schema.
        original_read = HOST_RELAY_ADAPTER._read_document
        def capture(*args, **kwargs):
            document = original_read(*args, **kwargs)
            if document is not None:
                envelopes.put(document)
            return document

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(type(HOST_RELAY_ADAPTER), "_read_document", staticmethod(capture))
            thread = threading.Thread(target=respond)
            thread.start()
            if failed:
                with pytest.raises(AutoIngestPolicyError, match="relay_reported_failure:relay_synthetic_failure"):
                    HOST_RELAY_ADAPTER.generate(handle, request, stop, None)
            else:
                output = HOST_RELAY_ADAPTER.generate(handle, request, stop, None)
                assert output["job_id"] == fixture.job
            thread.join(timeout=3)
            assert not thread.is_alive()
            assert errors.empty(), list(errors.queue)
            document = envelopes.get_nowait()
            assert set(document) == {"protocol", "attempt_id", "lease_generation", "nonce", "failed" if failed else "output"}
            assert document["attempt_id"] == fixture.attempt
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=3)
        fixture.tearDown()
