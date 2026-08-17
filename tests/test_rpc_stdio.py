"""The pipe belongs to the protocol, and only to the protocol.

These run the sidecar's stdio isolation in a SUBPROCESS on purpose.
`_take_stdio` calls `os.dup2` on fds 0 and 1, and doing that in-process
would redirect pytest's own output for the rest of the session -- the
test would pass and every later failure would be invisible.

The rude session below is not invented. It is what `kaggle` does when it
cannot find credentials, reached through `poll`: it prints a page of
authentication help to stdout and then reads stdin for the answer. On
2026-08-17 that took down a live Electron window -- the banner arrived as
sixteen unreadable lines, a queued call lost its reply to the prompt, and
the read loop then saw the EOF as "the shell is gone" and exited 0.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Imported inside the child rather than at module scope: importing
# blendfleet.rpc.__main__ here would pull the whole session in for a test
# that never uses it.
CHILD = """
import sys
from blendfleet.rpc.__main__ import _take_stdio, serve

class Rude:
    def greet(self):
        print("a banner nobody asked for")
        return "read:" + repr(sys.stdin.readline())

    def stop(self):
        pass

protocol_in, protocol_out = _take_stdio({log!r})
raise SystemExit(serve(Rude(), protocol_in, protocol_out))
"""


def _run(lines, log=None):
    done = subprocess.run(
        [sys.executable, "-c", CHILD.format(log=log)],
        input="".join(lines), capture_output=True, text=True, cwd=ROOT,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done


def test_a_print_inside_a_call_never_reaches_the_pipe():
    """Every line on stdout parses as a protocol message, or the shell on
    the other end cannot tell a banner from a corrupt reply."""
    done = _run(['{"id": 1, "call": "greet"}\n'])
    written = [line for line in done.stdout.splitlines() if line.strip()]
    assert written, "the reply itself went missing"
    for line in written:
        json.loads(line)          # raises, with the offending line, if not


def test_a_prompt_inside_a_call_gets_eof_not_the_next_message():
    """The failure that cost a reply: the second line must still be the
    protocol's to read, and it must still be answered."""
    done = _run(['{"id": 1, "call": "greet"}\n',
                 '{"id": 2, "call": "greet"}\n'])
    replies = {json.loads(line)["id"]: json.loads(line)
               for line in done.stdout.splitlines() if line.strip()}

    assert replies[1]["result"] == "read:''", (
        "the call read something off stdin -- it should have seen EOF")
    assert 2 in replies, "the second call was eaten by the first one's prompt"
    assert replies[2]["ok"] is True


def test_the_stray_print_is_kept_in_the_log_rather_than_dropped(tmp_path):
    """Silencing a library is not the same as hiding what it said. A real
    authentication problem has to stay findable after the fact."""
    log = tmp_path / "diagnostic.log"
    log.write_text("something already here\n", encoding="utf-8")
    _run(['{"id": 1, "call": "greet"}\n'], log=str(log))

    kept = log.read_text(encoding="utf-8")
    assert "a banner nobody asked for" in kept
    assert kept.startswith("something already here"), "appended, not truncated"


def test_an_unwritable_log_still_gets_the_pipe_closed_off(tmp_path):
    """The fallback path: if the log cannot be opened, the protocol is
    still protected. A directory is never openable as a file."""
    done = _run(['{"id": 1, "call": "greet"}\n'], log=str(tmp_path))
    for line in done.stdout.splitlines():
        if line.strip():
            json.loads(line)
