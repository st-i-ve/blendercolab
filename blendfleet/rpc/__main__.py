"""The sidecar: BlendFleet's backend, with a pipe where its window was.

    python -m blendfleet.rpc

Reads one JSON object per line on stdin, writes replies and events to
stdout (see protocol.py). Nothing else goes to stdout, ever -- a stray
print would land in the middle of the stream and read as a corrupt
message. Logging goes to the diagnostic file the rest of the app uses.

EOF ON STDIN IS THE SHUTDOWN SIGNAL. It is what the sidecar gets when
the shell that spawned it dies -- including when it dies badly -- and
acting on it is what stops an orphaned Python process holding a render's
state with no window left to show it. There is no other exit path: no
"quit" call to forget to send, no timeout to tune.
"""
from __future__ import annotations

import sys

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

    def write(line: str) -> None:
        try:
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
    try:
        session = build_session()
    except Exception as e:      # noqa: BLE001 -- the shell has to hear this
        crash_log.record(f"the sidecar could not start: {type(e).__name__}: {e}",
                         critical=True)
        # Said on the protocol as well as in the log: a shell waiting for
        # a reply that never comes shows a blank window with no reason.
        sys.stdout.write('{"event":"notification","args":['
                         f'"BlendFleet could not start its backend: {e}",'
                         '"offline"]}\n')
        sys.stdout.flush()
        return 1
    return serve(session)


if __name__ == "__main__":
    raise SystemExit(main())
