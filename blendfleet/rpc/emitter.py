"""The three Qt primitives the adapter needs, without Qt.

`ui/bridge.py` is a QObject: it answers the page through Signals, runs
network calls on QThreads, and polls Kaggle on QTimers. None of that is
available to a process that has to run headless beside an Electron shell,
and none of it is needed -- what the page actually consumes is "call a
method, get JSON back" and "hand me an event when something changes".

So this module provides the same three shapes:

    Signal   -> Emitter          connect / disconnect / emit
    QThread  -> Worker           start / wait, answering with Emitters
    QTimer   -> RepeatingTimer   start / stop, a callback on an interval

They are deliberately the SAME API, not a better one. tests/test_bridge.py
drives the Qt adapter through `.connect()` and `.wait()`, and the whole
point of keeping those names is that the same tests can be pointed at
this side to prove the two agree.

THREADING. Qt delivers a queued signal on the receiving object's thread;
these deliver on the emitting thread. That difference is why this is a
separate implementation rather than a shim: the sidecar serialises every
outbound message through one writer lock (see protocol.write), so a
handler racing another handler cannot corrupt the stream, and nothing
here ever touches a widget.
"""
from __future__ import annotations

import threading
import traceback
from typing import Callable


class Emitter:
    """A signal: any number of handlers, called in the order connected.

    A handler that raises must not stop the ones behind it, nor kill the
    worker thread that emitted -- an exception in a UI handler taking the
    render's progress stream down with it would be the worst kind of
    coupling. It is recorded and stepped over.
    """

    __slots__ = ("_handlers", "_lock", "_name")

    def __init__(self, name: str = "") -> None:
        self._handlers: list[Callable] = []
        self._lock = threading.Lock()
        self._name = name

    def connect(self, handler: Callable) -> None:
        with self._lock:
            self._handlers.append(handler)

    def disconnect(self, handler: Callable) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def emit(self, *args) -> None:
        # Copied under the lock, called outside it: a handler that
        # connects or disconnects another handler (the preview modal does
        # exactly this) must not deadlock on the emit that reached it.
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(*args)
            except Exception:       # noqa: BLE001 -- one bad handler only
                from blendfleet import crash_log
                crash_log.record(
                    f"a handler for {self._name or 'an event'} raised:\n"
                    + traceback.format_exc(), critical=False)

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return f"<Emitter {self._name!r} handlers={len(self._handlers)}>"


class Worker:
    """One off-thread call, answered through Emitters.

    The same contract as bridge._Worker: `succeeded(result)` or
    `failed(message)`, then `finished()` either way. `action` is the
    gerund phrase used to build the friendly sentence when `fn` raises,
    so the page never sees a traceback -- and the traceback still reaches
    the diagnostic log, because "Kaggle could not be reached" covers a
    DNS failure, a 403 and a bug in this app equally well.
    """

    def __init__(self, fn: Callable[[], object], action: str,
                 scrub: Callable[[str], str] | None = None) -> None:
        self._fn = fn
        self._action = action
        self._scrub = scrub or (lambda text: text)
        self.succeeded = Emitter("succeeded")
        self.failed = Emitter("failed")
        self.finished = Emitter("finished")
        # Daemon: a worker still inside a Kaggle request must never be
        # what keeps the process alive after the shell has gone. stop()
        # gives it a grace period first; see Session.stop.
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def isRunning(self) -> bool:            # noqa: N802 -- Qt's own spelling
        return self._thread.is_alive()

    def isFinished(self) -> bool:           # noqa: N802 -- Qt's own spelling
        """True once the call has returned, as QThread means it.

        Deliberately not `not isRunning()`: a worker that was never
        started is neither running nor finished, and answering True for it
        would let stop() report that it had waited for something that
        never ran.
        """
        return self._started and not self._thread.is_alive()

    def wait(self, milliseconds: int | None = None) -> bool:
        """Join, returning False if it is still running afterwards."""
        self._thread.join(None if milliseconds is None else milliseconds / 1000)
        return not self._thread.is_alive()

    def _run(self) -> None:
        from blendfleet import crash_log
        from blendfleet.ui.messages import explain
        try:
            result = self._fn()
        except Exception as e:      # noqa: BLE001 -- turned into a message
            crash_log.record(
                self._scrub(
                    f"a background call failed -- {self._action}: "
                    f"{type(e).__name__}: {e}\n"
                    + "".join(traceback.format_exception(
                        type(e), e, e.__traceback__))),
                critical=True)
            self.failed.emit(explain(self._action, e))
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class RepeatingTimer:
    """A callback on an interval, until stopped.

    One thread per timer, sleeping on an Event rather than on time.sleep,
    so stop() returns immediately instead of waiting out the interval --
    a 30-second poll timer that has to be waited out is 30 seconds of a
    window refusing to close.
    """

    def __init__(self, interval_ms: int, callback: Callable[[], None],
                 name: str = "timer") -> None:
        self._interval = interval_ms / 1000
        self._callback = callback
        self._stop = threading.Event()
        self._name = name
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=self._name)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(2)

    def settle(self, timeout: float = 1.0) -> None:
        """Cancel, then wait for a tick already in flight to finish --
        unless the caller IS that tick, in which case waiting is a deadlock
        and there is nothing to wait for anyway.

        The difference from stop() is only that this is safe to call from
        anywhere, including an atexit handler with no idea what is running.
        """
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def cancel(self) -> None:
        """Stop ticking, without waiting for the tick to finish.

        `stop()` joins, which is right when the shell is closing and wrong
        when the caller IS this timer's own callback -- joining your own
        thread raises. That case is real: the Qt adapter cancels these from
        inside a tick when it notices the window it was feeding has been
        destroyed (see bridge._forward).
        """
        self._stop.set()
        self._thread = None

    def _run(self) -> None:
        from blendfleet import crash_log
        while not self._stop.wait(self._interval):
            try:
                self._callback()
            except Exception:       # noqa: BLE001 -- a tick, not the app
                crash_log.record(
                    f"the {self._name} tick raised:\n" + traceback.format_exc(),
                    critical=False)
