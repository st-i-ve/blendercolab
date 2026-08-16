"""The pipe: what may cross it, and what must never close it.

The sidecar is the only thing standing between the page and the render,
so the failure that matters here is not "an error was returned" -- it is
"the stream stopped". Every test below is about the sidecar staying up
and staying honest when it is handed something it did not expect.

Driven against a stub session rather than the real one: this file is
about framing and dispatch, and a real Session would start timers,
poll Kaggle and write state files to prove nothing about either.
"""
import io
import json

import pytest

from blendfleet.rpc.__main__ import serve
from blendfleet.rpc.emitter import Emitter
from blendfleet.rpc.protocol import EVENTS, Dispatcher, encode


class StubSession:
    """Enough of a Session to dispatch against: a few methods, the events,
    and one that raises on purpose."""

    def __init__(self):
        for name in EVENTS:
            setattr(self, name, Emitter(name))
        self.calls = []

    def state(self):
        self.calls.append("state")
        return '{"instances": []}'

    def collect(self, label="", job_id="", destination=""):
        self.calls.append(("collect", label, job_id, destination))

    def addAccount(self, label, token):     # noqa: N802 - the page's name
        raise RuntimeError(f"Kaggle rejected {token}")

    def _scrub(self, text):
        return text.replace("KGAT_secret", "KGAT_***")

    def stop(self):
        self.calls.append("stop")


@pytest.fixture
def wired():
    session = StubSession()
    written = []
    dispatcher = Dispatcher(session, written.append)
    return session, dispatcher, [json.loads(line) for line in written], written


def _replies(written):
    return [json.loads(line) for line in written]


# ---------------- framing ----------------

def test_every_message_is_one_line_and_ends_with_one(wired):
    session, dispatcher, _, written = wired
    dispatcher.handle_line('{"id": 1, "call": "state"}')
    assert len(written) == 1
    assert written[0].endswith("\n")
    assert "\n" not in written[0][:-1], "a payload broke the line framing"


def test_non_ascii_is_escaped_rather_than_trusted_to_the_console(wired):
    """A Windows console at cp1252 raises on an unescaped é, and the
    sidecar dying because a scene was called naïve.blend would be an
    absurd way to lose a render."""
    assert "\\u" in encode({"event": "logLine", "args": ["naïve.blend"]})
    assert encode({"a": "é"}).isascii()


def test_the_id_comes_back_exactly_as_it_went_out(wired):
    session, dispatcher, _, written = wired
    for call_id in (7, "abc", None, 0):
        written.clear()
        dispatcher.handle_line(json.dumps({"id": call_id, "call": "state"}))
        assert _replies(written)[0]["id"] == call_id


# ---------------- refusing without closing ----------------

def test_an_unknown_call_is_answered_not_ignored(wired):
    """A page built against a newer sidecar would otherwise wait for ever
    for a reply that is never coming."""
    session, dispatcher, _, written = wired
    dispatcher.handle_line('{"id": 2, "call": "teleport"}')
    reply = _replies(written)[0]
    assert reply["ok"] is False
    assert "teleport" in reply["error"]


def test_a_private_name_is_not_reachable_from_the_page(wired):
    """Only the contract is callable. _scrub, __class__ and friends are
    not part of it."""
    session, dispatcher, _, written = wired
    for name in ("_scrub", "__class__", "__init__", ""):
        written.clear()
        dispatcher.handle_line(json.dumps({"id": 3, "call": name}))
        assert _replies(written)[0]["ok"] is False


def test_a_line_that_is_not_json_is_answered_and_the_stream_continues(wired):
    session, dispatcher, _, written = wired
    dispatcher.handle_line("this is not json")
    dispatcher.handle_line('{"id": 4, "call": "state"}')
    replies = _replies(written)
    assert replies[0] == {"id": None, "ok": False,
                          "error": replies[0]["error"]}
    assert replies[1]["ok"] is True, "the stream stopped after a bad line"


def test_a_json_scalar_is_refused_like_any_other_malformed_message(wired):
    session, dispatcher, _, written = wired
    dispatcher.handle_line("42")
    assert _replies(written)[0]["ok"] is False


def test_a_blank_line_says_nothing_at_all(wired):
    """Pipes carry blank lines; they are not messages and do not deserve
    an error each."""
    session, dispatcher, _, written = wired
    dispatcher.handle_line("")
    dispatcher.handle_line("   \n")
    assert written == []


# ---------------- errors ----------------

def test_a_method_that_raises_answers_instead_of_dying(wired):
    session, dispatcher, _, written = wired
    dispatcher.handle_line(
        '{"id": 5, "call": "addAccount", "args": ["acct", "KGAT_secret"]}')
    reply = _replies(written)[0]
    assert reply["ok"] is False
    assert "RuntimeError" in reply["error"]


def test_an_error_carries_no_token(wired):
    """The pipe is where a Kaggle token is at its most exposed -- it
    crosses it whenever an account is added -- so anything coming back
    goes through the session's own scrub."""
    session, dispatcher, _, written = wired
    dispatcher.handle_line(
        '{"id": 6, "call": "addAccount", "args": ["acct", "KGAT_secret"]}')
    assert "KGAT_secret" not in written[0]
    assert "KGAT_***" in written[0]


def test_missing_args_are_a_refusal_not_a_crash(wired):
    session, dispatcher, _, written = wired
    dispatcher.handle_line('{"id": 7, "call": "addAccount"}')
    assert _replies(written)[0]["ok"] is False


# ---------------- events ----------------

def test_every_event_the_page_listens_for_is_forwarded(wired):
    session, dispatcher, _, written = wired
    for name in EVENTS:
        written.clear()
        getattr(session, name).emit("payload")
        message = _replies(written)[0]
        assert message == {"event": name, "args": ["payload"]}


def test_a_multi_argument_event_keeps_its_shape(wired):
    """busyChanged is (key, bool) and logLine is (message, tone); a
    flattened or reordered pair would silently disable the render
    button."""
    session, dispatcher, _, written = wired
    session.busyChanged.emit("launch", True)
    assert _replies(written)[0]["args"] == ["launch", True]
    written.clear()
    session.logLine.emit("uploading", "warn")
    assert _replies(written)[0]["args"] == ["uploading", "warn"]


def test_an_event_and_a_reply_never_interleave(wired):
    """Events arrive from worker threads while a reply is being written.
    Two half-lines spliced together is a corrupt payload, so the writer
    is serialised -- this asserts each written chunk is a whole message."""
    session, dispatcher, _, written = wired
    session.stateChanged.emit("{}")
    dispatcher.handle_line('{"id": 8, "call": "state"}')
    session.telemetry.emit("{}")
    for line in written:
        json.loads(line)        # each chunk stands alone or this raises


def test_a_handler_that_raises_does_not_take_the_stream_down():
    """One bad listener must not stop the render's progress reaching the
    page -- Emitter records it and carries on."""
    session = StubSession()
    seen = []
    session.stateChanged.connect(lambda payload: (_ for _ in ()).throw(
        ValueError("bad handler")))
    session.stateChanged.connect(seen.append)
    session.stateChanged.emit("{}")
    assert seen == ["{}"]


# ---------------- the read loop ----------------
# EOF on stdin is the ONLY shutdown path. It is what the sidecar gets
# when the shell dies, including when it dies badly, and acting on it is
# what stops an orphaned Python process holding a render's state with no
# window left to show it.

def test_the_pipe_closing_stops_the_session():
    session = StubSession()
    assert serve(session, io.StringIO(""), io.StringIO()) == 0
    assert "stop" in session.calls


def test_the_session_is_stopped_even_if_a_call_was_mid_flight():
    """The shell can die at any moment, including in the middle of a
    line. Nothing is left running because of it."""
    session = StubSession()
    stdin = io.StringIO('{"id": 1, "call": "state"}\n'
                        '{"id": 2, "call": "sta')
    serve(session, stdin, io.StringIO())
    assert session.calls[0] == "state"
    assert "stop" in session.calls


def test_lines_arriving_together_are_all_dispatched():
    session = StubSession()
    stdout = io.StringIO()
    stdin = io.StringIO('{"id": 1, "call": "state"}\n'
                        '{"id": 2, "call": "state"}\n')
    serve(session, stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2]


def test_a_shell_that_vanishes_mid_reply_does_not_raise():
    """Writing to a closed pipe raises, and the sidecar has nowhere to
    report that to -- it is already over. It exits quietly instead."""
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("the shell is gone")

    session = StubSession()
    assert serve(session, io.StringIO('{"id": 1, "call": "state"}\n'),
                 Broken()) == 0
    assert "stop" in session.calls
