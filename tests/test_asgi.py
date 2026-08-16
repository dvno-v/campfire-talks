"""Production HTTP-edge checks without opening a network socket."""

import asyncio
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

temporary = tempfile.TemporaryDirectory()
os.environ.setdefault("CAMPFIRE_DB", str(Path(temporary.name) / "asgi.db"))
os.environ.setdefault("CAMPFIRE_UPLOAD_DIR", str(Path(temporary.name) / "uploads"))

from campfire.asgi import JSON_BODY_LIMIT, _ResponseBridge, application, serve


async def invoke(method, path, body=b"", headers=None):
    delivered = False
    wait_forever = asyncio.Event()
    messages = []

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await wait_forever.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers or [], "client": ("127.0.0.1", 12345),
    }
    await application(scope, receive, send)
    status = next(message["status"] for message in messages
                  if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in messages
                             if message["type"] == "http.response.body")
    return status, messages, response_body


class ASGITests(unittest.TestCase):
    def test_health_request_runs_through_the_production_adapter(self):
        status, messages, body = asyncio.run(invoke("GET", "/healthz"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        headers = dict(next(message["headers"] for message in messages
                            if message["type"] == "http.response.start"))
        self.assertIn(b"content-security-policy", headers)
        self.assertNotIn(b"connection", headers)

    def test_edge_rejects_oversized_json_before_allocating_a_handler(self):
        status, _, body = asyncio.run(invoke(
            "POST", "/api/login", b"x" * (JSON_BODY_LIMIT + 1)))
        self.assertEqual(status, 413)
        self.assertIn("too large", json.loads(body)["error"])

    def test_parsed_request_body_has_unambiguous_framing(self):
        headers = [(b"transfer-encoding", b"chunked"), (b"content-length", b"999")]
        status, _, body = asyncio.run(invoke(
            "POST", "/api/login", b"{}", headers=headers))
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "Incorrect username or password")

    def test_response_bridge_hands_every_chunk_over_in_order(self):
        """The sender waits on an event rather than a timer.

        An event stream stays open for hours while producing almost nothing, so
        the handoff has to cost nothing while it is idle — and still lose
        nothing when a chunk is queued during a drain.
        """
        async def scenario():
            bridge = _ResponseBridge(asyncio.get_running_loop())
            produced = [("start", 200, [])]
            produced += [("body", str(index).encode()) for index in range(40)]
            produced.append(("done", None))
            writer = threading.Thread(target=lambda: [bridge.put(item) for item in produced])
            writer.start()
            collected = []
            while not (collected and collected[-1][0] == "done"):
                await asyncio.wait_for(bridge.wait(), timeout=5)
                while True:
                    try:
                        collected.append(bridge.items.get_nowait())
                    except queue.Empty:
                        break
            writer.join(timeout=5)
            return collected, produced

        collected, produced = asyncio.run(scenario())
        self.assertEqual(collected, produced)

    def test_response_bridge_refuses_to_write_a_body_to_a_gone_client(self):
        async def scenario():
            bridge = _ResponseBridge(asyncio.get_running_loop())
            bridge.disconnected.set()
            return bridge

        bridge = asyncio.run(scenario())
        with self.assertRaises(BrokenPipeError):
            bridge.put(("body", b"nobody is listening"))

    def test_uvicorn_process_has_finite_connection_and_timeout_settings(self):
        with patch("uvicorn.run") as run:
            serve()
        settings = run.call_args.kwargs
        self.assertEqual(settings["workers"], 1)
        self.assertGreater(settings["limit_concurrency"], 0)
        self.assertEqual(settings["backlog"], settings["limit_concurrency"])
        self.assertGreater(settings["timeout_keep_alive"], 0)
        self.assertGreater(settings["h11_max_incomplete_event_size"], 0)
        self.assertFalse(settings["proxy_headers"])


if __name__ == "__main__":
    unittest.main()
