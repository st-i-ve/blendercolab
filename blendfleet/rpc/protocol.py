"""What travels down the pipe, and what is allowed to come back up.

One JSON object per line, both directions:

    page -> sidecar   {"id": 7, "call": "launch", "args": ["{...}"]}
    in reply          {"id": 7, "ok": true, "result": "{...}"}
                      {"id": 7, "ok": false, "error": "no machines selected"}
    unsolicited       {"event": "stateChanged", "args": ["{...}"]}

Newline-delimited rather than length-prefixed because every payload the
adapter produces is already a JSON string with no raw newlines in it,
and a format a human can read by piping the sidecar into a terminal is
worth more here than a few saved bytes.

THE RULES THAT MATTER, all of them about not going silent:

  - An unknown method answers `ok: false`. It never closes the pipe: one
    stale call from a page built against a newer sidecar would otherwise
    take the whole window down with it.
  - An exception inside a method answers `ok: false` with the message,
    scrubbed of tokens. The traceback goes to the diagnostic log, where
    a support request can find it.
  - A line that will not parse is answered too (`id: null`), and the
    stream continues.
  - `id` is echoed exactly as it arrived, including if it is a string or
    absent -- matching is the caller's business, not this module's.
"""
from __future__ import annotations

import json
import threading
import traceback
from typing import Callable

# Every event a Session can emit. Named explicitly rather than discovered
# by walking the object: an attribute that happens to have connect() is
# not a promise, and this list IS the contract the page is written
# against (see tests/test_rpc_session.py, which asserts both adapters
# carry exactly these).
EVENTS = (
    "stateChanged", "accountsChanged", "settingsChanged", "telemetry",
    "uploadProgress", "downloadProgress", "framePreview", "logLine",
    "notification", "healthChanged", "busyChanged", "scenesChanged",
    "outputsChanged", "collectFinished", "straySessionsChanged",
)


def encode(message: dict) -> str:
    """One line, terminated. Non-ASCII is escaped so the pipe is safe
    whatever the console's code page is -- a Windows terminal at cp1252
    would otherwise raise on a scene called `naïve.blend`."""
    return json.dumps(message, ensure_ascii=True) + "\n"


class Dispatcher:
    """Turns lines into method calls, and events into lines.

    Owns neither the session nor the transport: it is handed a session
    and a `write(str)`, which is what lets the tests drive it with a list
    for a pipe.
    """

    def __init__(self, session, write: Callable[[str], None],
                 scrub: Callable[[str], str] | None = None) -> None:
        self._session = session
        self._write = write
        self._scrub = scrub or getattr(session, "_scrub", None) or (lambda t: t)
        # One writer, one lock. Events arrive from worker threads and from
        # the SSE streams; two half-written lines interleaved would be a
        # protocol error that looks like a corrupt payload.
        self._lock = threading.Lock()
        self._connect_events()

    # ---- outbound ----------------------------------------------------
    def send(self, message: dict) -> None:
        line = encode(message)
        with self._lock:
            self._write(line)

    def _connect_events(self) -> None:
        for name in EVENTS:
            event = getattr(self._session, name, None)
            if event is None:           # pragma: no cover - guarded by tests
                continue
            event.connect(self._forward(name))

    def _forward(self, name: str) -> Callable:
        def handler(*args) -> None:
            self.send({"event": name, "args": list(args)})
        return handler

    # ---- inbound -----------------------------------------------------
    def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError as e:
            self.send({"id": None, "ok": False,
                       "error": f"could not read that as JSON: {e}"})
            return
        if not isinstance(message, dict):
            self.send({"id": None, "ok": False,
                       "error": "a message must be an object"})
            return
        self.handle(message)

    def handle(self, message: dict) -> None:
        call_id = message.get("id")
        name = message.get("call")
        args = message.get("args") or []
        method = self._method(name)
        if method is None:
            self.send({"id": call_id, "ok": False,
                       "error": f"no such call: {name!r}"})
            return
        try:
            result = method(*args)
        except Exception as e:      # noqa: BLE001 -- reported, not raised
            from blendfleet import crash_log
            crash_log.record(
                self._scrub(f"the call {name!r} raised: {type(e).__name__}: "
                            f"{e}\n" + traceback.format_exc()),
                critical=True)
            self.send({"id": call_id, "ok": False,
                       "error": self._scrub(f"{type(e).__name__}: {e}")})
        else:
            self.send({"id": call_id, "ok": True, "result": result})

    def _method(self, name) -> Callable | None:
        """The named method, if the page is allowed to call it.

        Public names only, and only ones that exist on the session -- so
        a malformed or hostile line cannot reach `_scrub`, `__class__` or
        anything else that is not part of the contract.
        """
        if not isinstance(name, str) or not name or name.startswith("_"):
            return None
        method = getattr(self._session, name, None)
        return method if callable(method) else None
