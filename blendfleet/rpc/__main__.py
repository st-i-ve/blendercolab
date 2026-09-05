"""The sidecar: BlendFleet's backend, with a pipe where its window was.

    python -m blendfleet.rpc

Reads one JSON object per line on stdin, writes replies and events to
stdout (see protocol.py). Nothing else goes to stdout, ever -- a stray
print would land in the middle of the stream and read as a corrupt
message. Logging goes to the diagnostic file the rest of the app uses.

That rule used to be a convention this module kept, and conventions do
not bind libraries. `kaggle`, reached through `poll`, prints eighteen
lines of authentication help to stdout and reads stdin for the answer --
so the pipe carrying the protocol was being written into by a banner and
read out of by a prompt, which cost a reply and then the process. See
`_take_stdio`: the rule is now enforced rather than requested.

EOF ON STDIN IS THE SHUTDOWN SIGNAL. It is what the sidecar gets when
the shell that spawned it dies -- including when it dies badly -- and
acting on it is what stops an orphaned Python process holding a render's
state with no window left to show it. There is no other exit path: no
"quit" call to forget to send, no timeout to tune.
"""
from __future__ import annotations

import os
import sys
import threading

from blendfleet import crash_log
from blendfleet.accounts import AccountStore
from blendfleet.fleet import Fleet
from blendfleet.kaggle_client import KaggleClient, verify_token
from blendfleet.platform_paths import cache_dir
from blendfleet.rpc.protocol import Dispatcher
from blendfleet.rpc.session import Session
from blendfleet.settings import Settings


def build_session() -> Session:
    """The same wiring web_main.py does, minus the window.

    The label lookup is not incidental: tokens are unique per account, so
    this is what lets KaggleClient's identity check name the account
    rather than a masked token.
    """
    store = AccountStore.load()
    settings = Settings.load()

    def fleet_factory(accounts):
        labels = {a.token: a.label for a in accounts}
        return Fleet(accounts,
                     lambda t: KaggleClient(t, label=labels.get(t)),
                     cache_dir() / "work")

    return Session(store, fleet_factory, verify_token, settings)


def _take_stdio(log_path=None):
    """Hand the protocol private copies of the pipe, then take the pipe
    away from everything else in this process.

    Done at the file-descriptor level, not by rebinding `sys.stdout`:
    what has to be contained is a library, and a library may print
    through C, or hand fd 1 to a subprocess it spawns. `dup2` follows the
    pipe wherever it is passed; rebinding a Python attribute does not.

    After this returns:

      - fd 1 is the diagnostic log, so a stray print is still readable
        afterwards rather than merely gone. Losing the Kaggle banner
        entirely is how a shipped build would hide a real auth problem.
      - fd 0 is the null device, so anything that prompts is answered
        with EOF immediately -- which surfaces as an honest `ok: false`
        on the call that prompted, instead of that call quietly eating
        the next message off the wire.

    Must run before anything reads stdin. Python's own `sys.stdin` has
    read nothing at this point; a chunk buffered into it ahead of the dup
    would be a message the protocol never sees.
    """
    protocol_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")

    empty = os.open(os.devnull, os.O_RDONLY)
    os.dup2(empty, 0)
    os.close(empty)

    try:
        sink = (os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                if log_path else os.open(os.devnull, os.O_WRONLY))
    except OSError:
        # An unwritable log is not a reason to leave the pipe exposed.
        sink = os.open(os.devnull, os.O_WRONLY)
    os.dup2(sink, 1)
    os.close(sink)

    return protocol_in, protocol_out


def serve(session: Session, stdin=None, stdout=None) -> int:
    """Read until the pipe closes, then stop.

    Dispatch happens on THIS thread, one line at a time. The methods
    themselves are already non-blocking -- anything that touches the
    network goes through Session._start onto a worker -- so a slow call
    cannot wedge the reader, and two calls cannot race each other into
    the same fleet file.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    # One line at a time onto the pipe, whoever is emitting. Dispatch is
    # single-threaded but EVENTS are not: they come from whichever worker
    # thread produced them, and since 2026-09-05 collect fans its
    # downloads out across four threads that all tick downloadProgress at
    # once. write+flush is two calls, so without this two events can
    # interleave into one another and hand the shell half a JSON object
    # -- and this protocol is newline-delimited, so a torn line is not a
    # dropped event, it is a parse error that ends the stream.
    write_lock = threading.Lock()

    def write(line: str) -> None:
        try:
            with write_lock:
                stdout.write(line)
                stdout.flush()
        except (BrokenPipeError, ValueError):
            # The shell went away mid-write. Nothing to report it to; the
            # read loop below is about to end for the same reason.
            pass

    dispatcher = Dispatcher(session, write)
    try:
        for line in stdin:
            dispatcher.handle_line(line)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        session.stop()
    return 0


def main() -> int:
    # The same diagnostic log the windowed builds write, and safe here:
    # install()'s Qt message handler reports False and moves on when Qt is
    # absent, which in this process it is. announce() is deliberately NOT
    # called -- it prints, and with no stderr it falls back to stdout,
    # which would put prose in the middle of the protocol.
    path = crash_log.install()
    crash_log.record(
        f"sidecar starting (python -m blendfleet.rpc), log at {path}")
    # Before build_session, which is the first thing here that imports a
    # library with opinions about the console.
    protocol_in, protocol_out = _take_stdio(path)
    try:
        session = build_session()
    except Exception as e:      # noqa: BLE001 -- the shell has to hear this
        crash_log.record(f"the sidecar could not start: {type(e).__name__}: {e}",
                         critical=True)
        # Said on the protocol as well as in the log: a shell waiting for
        # a reply that never comes shows a blank window with no reason.
        protocol_out.write('{"event":"notification","args":['
                           f'"BlendFleet could not start its backend: {e}",'
                           '"offline"]}\n')
        protocol_out.flush()
        return 1
    return serve(session, protocol_in, protocol_out)


if __name__ == "__main__":
    raise SystemExit(main())
