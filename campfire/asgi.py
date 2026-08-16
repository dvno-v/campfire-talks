"""Bounded ASGI edge for the synchronous Campfire request handlers.

Uvicorn owns HTTP parsing, connection timeouts, flow control, and the public
concurrency ceiling. The established handlers still run synchronously in a
fixed-size executor; this keeps the authorization surface stable while making
thread creation finite and replacing the standard-library development server.
"""

from __future__ import annotations

import asyncio
import io
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http import HTTPStatus

from .config import HOST, KEEPALIVE_TIMEOUT_SECONDS, MAX_CONCURRENT_REQUESTS
from .config import MAX_UPLOAD_BYTES, PORT, REQUEST_WORKERS
from .http import App, response_security_headers

JSON_BODY_LIMIT = 70_000
H11_MAX_INCOMPLETE_EVENT_SIZE = 32 * 1024
_EXECUTORS = {}
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}


def _executor_for(loop):
    # Python 3.14 captures execution context when an executor is constructed.
    # Creating the bounded pool inside its owning event loop also makes the
    # ASGI callable reliable in test runners that create more than one loop.
    executor = _EXECUTORS.get(loop)
    if executor is None:
        executor = ThreadPoolExecutor(
            max_workers=REQUEST_WORKERS, thread_name_prefix="request")
        _EXECUTORS[loop] = executor
    return executor


class _ResponseBridge:
    """Apply bounded backpressure between a handler thread and ASGI.

    The handler runs in a worker thread and the sender runs on the event loop,
    so the queue needs a wake-up the loop can wait on. An event stream stays
    open for hours while producing almost nothing, and polling it would cost a
    timer wake-up per stream several times a second for its whole life.
    """

    def __init__(self, loop):
        self.items = queue.Queue(maxsize=16)
        self.disconnected = threading.Event()
        self._loop = loop
        self._ready = asyncio.Event()

    def _wake(self):
        """Signal the loop's event from a handler thread."""
        try:
            self._loop.call_soon_threadsafe(self._ready.set)
        except RuntimeError:
            # The loop is already closing; nothing is left to wake.
            pass

    async def wait(self):
        """Block until at least one item may be waiting.

        Cleared before the caller drains, so an item queued during the drain
        leaves the event set and the next wait returns immediately rather than
        stranding a response.
        """
        await self._ready.wait()
        self._ready.clear()

    def put(self, item):
        if self.disconnected.is_set() and item[0] == "body":
            raise BrokenPipeError
        while True:
            try:
                self.items.put(item, timeout=1)
                self._wake()
                return
            except queue.Full:
                pass
            if self.disconnected.is_set() and item[0] == "body":
                raise BrokenPipeError

    def write(self, content):
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("HTTP response writes must be bytes")
        content = bytes(content)
        if content:
            self.put(("body", content))
        return len(content)

    def flush(self):
        return None


class ASGIRequest(App):
    """Present one parsed ASGI request to the existing handler methods."""

    def __init__(self, scope, body, output):
        self.command = scope["method"].upper()
        raw_path = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
        self.path = raw_path.decode("latin-1")
        query = scope.get("query_string", b"")
        if query:
            self.path += "?" + query.decode("latin-1")
        self.request_version = "HTTP/1.1"
        self.requestline = f"{self.command} {self.path} {self.request_version}"
        self.close_connection = False
        self.client_address = tuple(scope.get("client") or ("", 0))
        self.headers = Message()
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin-1")
            if name.casefold() in {"content-length", "transfer-encoding"}:
                continue
            self.headers.add_header(name, raw_value.decode("latin-1"))
        if body or self.command in {"POST", "PUT", "PATCH", "DELETE"}:
            self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)
        self.wfile = output
        self._output = output
        self._response_status = None
        self._response_headers = []
        self._response_started = False

    def send_response(self, code, message=None):
        del message
        self._response_status = int(code)

    def send_header(self, keyword, value):
        self._response_headers.append((str(keyword), str(value)))

    def end_headers(self):
        if self._response_started:
            return
        self.add_security_headers()
        status = self._response_status or HTTPStatus.OK
        headers = [(name, value) for name, value in self._response_headers
                   if name.casefold() not in _HOP_BY_HOP]
        self._output.put(("start", status, headers))
        self._response_started = True

    def client_disconnected(self):
        return self._output.disconnected.is_set()

    def log_message(self, fmt, *args):
        # App owns its privacy-preserving access-log behavior. Avoid the base
        # class implementation, which assumes socket-derived request state.
        if getattr(self, "requestline", None):
            super().log_message(fmt, *args)


def _request_body_limit(scope):
    path = scope.get("path", "")
    if scope.get("method", "").upper() == "POST" and path.endswith("/uploads"):
        return MAX_UPLOAD_BYTES
    return JSON_BODY_LIMIT


async def _read_body(scope, receive):
    maximum = _request_body_limit(scope)
    body = bytearray()
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            return None, False
        if event["type"] != "http.request":
            continue
        chunk = event.get("body", b"")
        if len(body) + len(chunk) > maximum:
            return None, True
        body.extend(chunk)
        if not event.get("more_body", False):
            return bytes(body), False


async def _send_error(send, status, message):
    body = json.dumps({"error": message}, separators=(",", ":")).encode()
    headers = [(b"content-type", b"application/json; charset=utf-8"),
               (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")]
    headers += [(name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in response_security_headers()]
    await send({"type": "http.response.start", "status": int(status), "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _run_request(request, output):
    try:
        handler = getattr(request, f"do_{request.command}", None)
        if handler is None:
            request.error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
        else:
            handler()
        if not request._response_started:
            request.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Request did not produce a response")
        output.put(("done", None))
    except (BrokenPipeError, ConnectionResetError):
        output.disconnected.set()
        output.put(("done", None))
    except Exception as failure:  # noqa: BLE001 - convert handler failures to a closed response
        output.put(("done", failure))


async def application(scope, receive, send):
    """ASGI application callable used by the production server and tests."""
    if scope["type"] != "http":
        return
    body, too_large = await _read_body(scope, receive)
    if too_large:
        await _send_error(send, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large")
        return
    if body is None:
        return

    loop = asyncio.get_running_loop()
    output = _ResponseBridge(loop)
    request = ASGIRequest(scope, body, output)
    future = _executor_for(loop).submit(_run_request, request, output)
    response_started = False
    send_failed = False

    async def watch_disconnect():
        while not output.disconnected.is_set():
            event = await receive()
            if event["type"] == "http.disconnect":
                output.disconnected.set()
                return

    watcher = asyncio.create_task(watch_disconnect())
    finished = False
    try:
        while not finished:
            await output.wait()
            while not finished:
                try:
                    item = output.items.get_nowait()
                except queue.Empty:
                    break
                kind = item[0]
                if kind == "start":
                    response_started = True
                    headers = [(name.lower().encode("latin-1"), value.encode("latin-1"))
                               for name, value in item[2]]
                    try:
                        await send({"type": "http.response.start", "status": int(item[1]),
                                    "headers": headers})
                    except Exception:  # client transport is already gone
                        send_failed = True
                        output.disconnected.set()
                elif kind == "body":
                    if not send_failed:
                        try:
                            await send({"type": "http.response.body", "body": item[1],
                                        "more_body": True})
                        except Exception:
                            send_failed = True
                            output.disconnected.set()
                elif kind == "done":
                    failure = item[1]
                    if failure and not response_started:
                        await _send_error(send, HTTPStatus.INTERNAL_SERVER_ERROR,
                                          "The request could not be completed")
                        response_started = True
                    if response_started and not send_failed:
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    finished = True
    finally:
        output.disconnected.set()
        watcher.cancel()
        future.cancel()


def serve():
    """Run one bounded process; Campfire's in-memory broker is single-process."""
    import uvicorn

    uvicorn.run(
        application,
        host=HOST,
        port=PORT,
        workers=1,
        limit_concurrency=MAX_CONCURRENT_REQUESTS,
        backlog=MAX_CONCURRENT_REQUESTS,
        timeout_keep_alive=KEEPALIVE_TIMEOUT_SECONDS,
        timeout_graceful_shutdown=15,
        h11_max_incomplete_event_size=H11_MAX_INCOMPLETE_EVENT_SIZE,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        lifespan="off",
    )
