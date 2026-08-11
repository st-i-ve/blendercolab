"""Regression tests for CRITICAL C2: tokens must never race between threads.

The bug: every auth path wrote os.environ["KAGGLE_API_TOKEN"] and read it
back after an import plus an HTTPS client construction. The dashboard starts
one SSE thread per account back-to-back through that path, so with a
realistic gap most threads authenticated with ANOTHER account's token and
then asked for a private kernel's logs they had no right to. The 403 was
swallowed, so live progress silently worked for at most one account.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

import blendfleet.log_stream as log_stream
from blendfleet.kaggle_client import ENV_TOKEN, _default_sdk_factory, _with_env_token

TOKENS = ["KGAT_" + c * 32 for c in "abcde"]

# Wide enough that an unsynchronised set-then-read is overwhelmingly likely
# to observe another thread's write (the review measured 4 of 5 accounts
# cross-authenticating with a 20 ms gap).
GAP = 0.02


def run_concurrently(fn, args_list):
    """Call fn(*args) once per entry, all threads released together."""
    results: dict[int, object] = {}
    errors: list[BaseException] = []
    start = threading.Barrier(len(args_list))

    def worker(i, args):
        try:
            start.wait()
            results[i] = fn(*args)
        except BaseException as e:      # noqa: BLE001 - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i, a))
               for i, a in enumerate(args_list)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    return [results[i] for i in range(len(args_list))]


# --------------------------------------------------------------------------
# kagglesdk: env-free, token passed as api_token=
# --------------------------------------------------------------------------

class FakeSdkClient:
    """Records the token it was constructed with, and whatever the global
    happened to be at that moment, after a realistic construction delay."""

    def __init__(self, api_token=None, **kw):
        self.env_at_construct_start = os.environ.get(ENV_TOKEN)
        time.sleep(GAP)
        self.api_token = api_token
        self.env_at_construct_end = os.environ.get(ENV_TOKEN)


@pytest.fixture
def fake_sdk(monkeypatch):
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", FakeSdkClient)
    return FakeSdkClient


def test_sdk_clients_built_concurrently_each_keep_their_own_token(fake_sdk):
    clients = run_concurrently(_default_sdk_factory, [(t,) for t in TOKENS])
    assert [c.api_token for c in clients] == TOKENS


def test_sdk_factory_never_touches_the_global_env(fake_sdk, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    clients = run_concurrently(_default_sdk_factory, [(t,) for t in TOKENS])
    # Not merely restored afterwards: never set at all, at any point during
    # any construction. Nothing leaks into child processes or crash dumps.
    assert all(c.env_at_construct_start is None for c in clients)
    assert all(c.env_at_construct_end is None for c in clients)
    assert ENV_TOKEN not in os.environ


# --------------------------------------------------------------------------
# kaggle.KaggleApi: no api_token= parameter exists, so lock-guarded instead
# --------------------------------------------------------------------------

def _construct_reading_env():
    """Stands in for KaggleApi() + authenticate(): reads the global, does a
    slow round trip (the real one calls _introspect_token over the network),
    then reads it again. Both reads must see the same token."""
    first = os.environ.get(ENV_TOKEN)
    time.sleep(GAP)
    return first, os.environ.get(ENV_TOKEN)


def test_env_token_helper_serialises_so_no_thread_sees_anothers_token():
    seen = run_concurrently(
        lambda tok: _with_env_token(tok, _construct_reading_env),
        [(t,) for t in TOKENS])
    for token, (first, second) in zip(TOKENS, seen):
        assert first == token, "read another account's token before the delay"
        assert second == token, "another thread overwrote the token mid-auth"


def test_env_token_helper_restores_the_previous_value(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN, "KGAT_" + "f" * 32)
    _with_env_token(TOKENS[0], lambda: None)
    assert os.environ[ENV_TOKEN] == "KGAT_" + "f" * 32


def test_env_token_helper_leaves_no_token_behind_when_none_was_set(monkeypatch):
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    _with_env_token(TOKENS[0], lambda: None)
    assert ENV_TOKEN not in os.environ


def test_env_token_helper_restores_even_when_construction_raises(monkeypatch):
    monkeypatch.delenv(ENV_TOKEN, raising=False)

    def boom():
        raise RuntimeError("auth exploded")

    with pytest.raises(RuntimeError):
        _with_env_token(TOKENS[0], boom)
    assert ENV_TOKEN not in os.environ


# --------------------------------------------------------------------------
# log_stream: the site the dashboard hits N times in parallel
# --------------------------------------------------------------------------

class FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


class RecordingStreamClient:
    """Captures (token, kernel_slug) pairs actually used together."""
    calls: list[tuple[str, str]] = []

    def __init__(self, api_token=None, **kw):
        self.api_token = api_token
        time.sleep(GAP)          # HTTPS client construction, as in the wild
        outer = self

        class _ApiClient:
            @staticmethod
            def get_kernel_session_logs_stream(req):
                RecordingStreamClient.calls.append(
                    (outer.api_token, req.kernel_slug))
                return FakeStreamResponse([
                    'data: {"stream_name":"stdout",'
                    '"data":"PROGRESS frame=1 ok=True secs=1.0 done=1/2\\n"}',
                    "data: END_OF_LOG",
                ])

        class _Kernels:
            kernels_api_client = _ApiClient()

        self.kernels = _Kernels()


def test_stream_progress_binds_each_thread_to_its_own_token(monkeypatch):
    """THE regression: N SSE threads started back-to-back must each request
    their own account's logs with their own account's credential."""
    import kagglesdk
    monkeypatch.setattr(kagglesdk, "KaggleClient", RecordingStreamClient)
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    RecordingStreamClient.calls = []

    pairs = [(t, f"user{i}", f"kernel-{i}") for i, t in enumerate(TOKENS)]
    run_concurrently(
        lambda tok, user, slug: log_stream.stream_progress(
            tok, user, slug, lambda done, total: None),
        pairs)

    expected = sorted((tok, slug) for tok, _user, slug in pairs)
    assert sorted(RecordingStreamClient.calls) == expected
    assert ENV_TOKEN not in os.environ


def test_no_module_writes_the_token_into_the_process_environment():
    """Static guard: an os.environ assignment reintroduced anywhere outside
    the single lock-guarded helper brings the whole race back."""
    import re
    from pathlib import Path
    assign = re.compile(
        r"""os\.environ\[\s*(?:ENV_TOKEN|["']KAGGLE_API_TOKEN["'])\s*\]\s*=[^=]""")
    root = Path(log_stream.__file__).parent
    offenders = [(path.name, line.strip())
                 for path in sorted(root.rglob("*.py"))
                 for line in path.read_text(encoding="utf-8").splitlines()
                 if assign.search(line)]
    # Two files may legitimately contain this text, for different reasons:
    #
    #   kaggle_client.py -- the set + restore pair inside _with_env_token,
    #     the single lock-guarded helper this whole guard exists to keep as
    #     the only one.
    #   notebook_builder.py -- ONE occurrence, inside the string template
    #     for a warm worker's notebook. That line never executes in this
    #     process: it is source code shipped to a Kaggle kernel, which runs
    #     on Kaggle, in its own interpreter, with only that same account's
    #     own token. The race this guard prevents is between threads HERE
    #     sharing one process environment; a Kaggle session has no such
    #     neighbours.
    #
    # The runtime half of that claim is asserted separately below, because
    # a comment is not evidence.
    by_file: dict[str, int] = {}
    for name, _line in offenders:
        by_file[name] = by_file.get(name, 0) + 1
    assert by_file == {"kaggle_client.py": 2, "notebook_builder.py": 1}, \
        offenders


def test_building_a_worker_notebook_does_not_touch_this_process_environment():
    """The runtime counterpart of the exception allowed above.

    notebook_builder writes `os.environ[...] = <token>` into the notebook
    it generates. This proves that writing it is all it does -- the app's
    own environment is never assigned, so the cross-thread token race
    cannot come back through this door.
    """
    import os
    import tempfile
    from pathlib import Path

    from blendfleet.notebook_builder import RenderSettings, build

    before = os.environ.get("KAGGLE_API_TOKEN")
    token = "KGAT_" + "b" * 32
    out = Path(tempfile.mkdtemp())
    path = build([], RenderSettings(1920, 1080, 128), "me/scene-blend",
                 out, "me/scene-worker-1", mode="worker",
                 control_slug="me/ctl", token=token, worker_label="acct0")

    assert os.environ.get("KAGGLE_API_TOKEN") == before
    # And the token really did reach the generated notebook -- otherwise
    # this test would pass for the wrong reason.
    assert token in path.read_text(encoding="utf-8")
