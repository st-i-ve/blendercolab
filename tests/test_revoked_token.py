"""Noticing a revoked token -- and, just as much, NOT crying wolf.

Requested directly: "make sure the system can sense a key is revoked".

The hard half is the false positives. This app has already made both
mistakes available to it:

  * an earlier version reported a genuinely revoked token as "this
    account owns nothing", sending the user to fix the wrong thing;
  * and on 2026-08-11/12 one machine's link dropped connections, reset
    sockets and failed SSL handshakes repeatedly -- if any of that were
    read as "revoked", the user would be told to replace three perfectly
    good keys.

Kaggle also uses 403 for two unrelated things: bad credentials, and a
dataset this account cannot see. The second is a NORMAL state while
collaborator sharing propagates, and it must never be reported as a dead
account.
"""
from __future__ import annotations

import pytest
import requests

from blendfleet.kaggle_client import (KaggleClient, KaggleError,
                                      KaggleTimeoutError, RevokedTokenError,
                                      _is_revoked_token)


def http_error(status: int, message: str = "") -> requests.HTTPError:
    """An HTTPError shaped like the ones Kaggle actually raises."""
    response = requests.Response()
    response.status_code = status
    err = requests.HTTPError(message or f"{status} Client Error", response=response)
    return err


# --------------------------------------------------------------------------
# What counts
# --------------------------------------------------------------------------

def test_a_401_is_a_revoked_token():
    assert _is_revoked_token(http_error(401, "401 Client Error: Unauthorized"))


def test_a_401_counts_even_with_no_useful_message():
    assert _is_revoked_token(http_error(401))


def test_an_auth_endpoint_403_is_a_revoked_token():
    # Kaggle's token introspection RPC -- seen in real tracebacks as
    # ".../v1/security.OAuthService/IntrospectToken".
    assert _is_revoked_token(http_error(
        403, "403 Client Error: Forbidden for url: "
             "https://api.kaggle.com/v1/security.OAuthService/IntrospectToken"))


def test_an_explicit_message_counts_without_any_status():
    # Some library errors arrive as a bare exception with no response.
    assert _is_revoked_token(RuntimeError("Invalid token"))
    assert _is_revoked_token(RuntimeError("this token has been revoked"))


# --------------------------------------------------------------------------
# What must NOT count -- every one of these happened on a real run
# --------------------------------------------------------------------------

def test_a_dataset_403_is_not_a_revoked_token():
    """The most dangerous false positive.

    dataset_reachable's own docstring: "A missing or invisible dataset
    raises HTTPError 403, not 404". That fires routinely while a
    collaborator share propagates -- reporting it as a dead key would
    send the user to regenerate a token that works.
    """
    assert not _is_revoked_token(http_error(
        403, "403 Client Error: Forbidden for url: "
             "https://www.kaggle.com/api/v1/datasets/view/someone/scene"))


@pytest.mark.parametrize("exc", [
    ConnectionResetError(10054, "An existing connection was forcibly closed"),
    requests.exceptions.ConnectionError("Connection aborted."),
    requests.exceptions.SSLError(
        "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of "
        "protocol"),
    requests.exceptions.ReadTimeout("Read timed out."),
    requests.exceptions.ChunkedEncodingError("Response ended prematurely"),
    OSError("[WinError 10061] No connection could be made"),
])
def test_a_flaky_connection_is_never_a_revoked_token(exc):
    """Every one of these was hit for real on 2026-08-11/12."""
    assert not _is_revoked_token(exc)


@pytest.mark.parametrize("status", [400, 404, 409, 429, 500, 502, 503])
def test_other_http_statuses_are_not_revoked_tokens(status):
    # 429 and 5xx especially: those mean "try later", the opposite of
    # "this key is dead". 409 is the conflict a concurrent push returns.
    assert not _is_revoked_token(http_error(status))


def test_a_missing_camera_is_not_a_revoked_token():
    # Sanity: render failures have nothing to do with credentials.
    assert not _is_revoked_token(RuntimeError("ERROR Cannot render, no camera"))


# --------------------------------------------------------------------------
# Through the client
# --------------------------------------------------------------------------

class FakeApi:
    def __init__(self, error):
        self._error = error

    def kernels_list(self, mine=False, page_size=1):
        raise self._error

    def kernels_status(self, slug):
        raise self._error


def client_raising(error, label="stive"):
    return KaggleClient("KGAT_" + "a" * 32, label=label,
                        api_factory=lambda tok: FakeApi(error))


def test_whoami_names_a_revoked_token():
    with pytest.raises(RevokedTokenError) as info:
        client_raising(http_error(401)).whoami()
    text = str(info.value)
    assert "revoked" in text
    assert "Generate New Token" in text, "must say what to do next"
    assert "stive" in text, "must say WHICH account"


def test_whoami_does_not_blame_the_token_for_a_dropped_connection():
    err = requests.exceptions.ConnectionError("Connection aborted.")
    with pytest.raises(Exception) as info:
        client_raising(err).whoami()
    assert not isinstance(info.value, RevokedTokenError)


def test_status_names_a_revoked_token_mid_render():
    """poll() runs every 30s for a job's whole life, so a token revoked
    part-way through surfaces here. The generic message says "usually a
    transient network problem -- will try again on the next check", which
    for this cause is precisely wrong."""
    with pytest.raises(RevokedTokenError):
        client_raising(http_error(401)).status("me/render-1")


def test_status_still_calls_a_network_blip_transient():
    err = requests.exceptions.ConnectionError("Connection aborted.")
    with pytest.raises(KaggleError) as info:
        client_raising(err).status("me/render-1")
    assert not isinstance(info.value, RevokedTokenError)
    assert "transient" in str(info.value)


def test_a_revoked_token_is_still_a_kaggle_error():
    """Every existing `except KaggleError` handler must keep working --
    a sibling type would have escaped all of them silently."""
    assert issubclass(RevokedTokenError, KaggleError)
    with pytest.raises(KaggleError):
        client_raising(http_error(401)).whoami()


def test_the_token_itself_is_never_put_in_the_message():
    with pytest.raises(RevokedTokenError) as info:
        client_raising(http_error(401), label=None).whoami()
    full = "KGAT_" + "a" * 32
    assert full not in str(info.value), "a token must never reach a message"
    assert "KGAT_aaaa" in str(info.value), "a masked prefix identifies it"


# --------------------------------------------------------------------------
# A network timeout is the newest way to be mistaken for a dead token
# --------------------------------------------------------------------------
#
# kaggle_client now installs a real read timeout, so "the connection went
# quiet" has become a routine, EXPECTED outcome of poll() rather than a
# thread that hangs forever. That makes the false-positive risk this whole
# module is about strictly worse: there is now a new exception type
# arriving on the same code path that reports revoked tokens.

TIMEOUT_ERRORS = [
    requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='www.kaggle.com', port=443): Read timed "
        "out. (read timeout=60.0)"),
    requests.exceptions.ConnectTimeout(
        "HTTPSConnectionPool(host='www.kaggle.com', port=443): Max retries "
        "exceeded (Caused by ConnectTimeoutError)"),
]


@pytest.mark.parametrize("exc", TIMEOUT_ERRORS)
def test_a_timeout_is_never_a_revoked_token(exc):
    assert not _is_revoked_token(exc)


@pytest.mark.parametrize("exc", TIMEOUT_ERRORS)
def test_status_reports_a_timeout_as_transient_not_revoked(exc):
    with pytest.raises(KaggleError) as info:
        client_raising(exc).status("me/render-1")
    assert isinstance(info.value, KaggleTimeoutError)
    assert not isinstance(info.value, RevokedTokenError), \
        "a quiet socket must never be reported as a dead key"


@pytest.mark.parametrize("exc", TIMEOUT_ERRORS)
def test_whoami_reports_a_timeout_as_transient_not_revoked(exc):
    with pytest.raises(KaggleError) as info:
        client_raising(exc).whoami()
    assert isinstance(info.value, KaggleTimeoutError)
    assert not isinstance(info.value, RevokedTokenError)


def test_a_timeout_wrapped_in_another_exception_is_still_a_timeout():
    """The timeout is installed on the session UNDERNEATH kagglesdk, so
    what reaches kaggle_client is usually a library exception raised FROM
    a ReadTimeout -- never the ReadTimeout itself. Matching only the
    outermost type would miss every real occurrence."""
    inner = requests.exceptions.ReadTimeout("Read timed out.")
    outer = RuntimeError("kernels_status failed")
    outer.__cause__ = inner
    with pytest.raises(KaggleTimeoutError):
        client_raising(outer).status("me/render-1")


def test_a_timeout_message_tells_the_user_what_to_do():
    """A bare "ReadTimeout" tells a user nothing. The message has to say
    what happened, that the account is fine, and what to do next."""
    with pytest.raises(KaggleTimeoutError) as info:
        client_raising(TIMEOUT_ERRORS[0]).status("me/render-1")
    text = str(info.value)
    assert "timed out" in text, "what happened"
    assert "retry" in text.lower(), "what to do next"
    assert "NOT a problem with this account or its token" in text, \
        "must actively rule out the token, not merely omit it"
    assert "nothing was lost" in text.lower() or "no work was lost" in text
    assert "ReadTimeout" not in text, "no raw exception type in a user string"


def test_a_timeout_is_still_a_kaggle_error():
    """Same contract RevokedTokenError has: every existing
    `except KaggleError` site keeps working unchanged."""
    assert issubclass(KaggleTimeoutError, KaggleError)
    assert not issubclass(KaggleTimeoutError, RevokedTokenError)
