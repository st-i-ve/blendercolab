"""Bounding how long a Kaggle HTTP call may block.

Lives in its own module because two unrelated callers need the same
injection point -- log_stream.py (the SSE progress stream) and
kaggle_client.py (every control-plane RPC) -- and a second copy of it
would be a second thing to fix the day kagglesdk renames an internal.
Deliberately imports nothing from blendfleet, so either of them can
depend on it without creating a cycle.
"""
from __future__ import annotations


def install_request_timeout(client, timeout) -> bool:
    """Give `client`'s requests.Session a default timeout.

    kagglesdk exposes no timeout parameter anywhere, so the only injection
    point is the Session it builds internally. Best-effort by design: if a
    future kagglesdk reshuffles its internals this returns False and the
    caller still runs (just without the backstop) rather than taking the
    app down over a private attribute. Tests inject fake clients
    everywhere, and a fake must fall down this same path silently.
    """
    try:
        http = client.http_client()
        http._init_session()
        session = http._session
        if session is None:
            return False
        original_send = session.send

        def send(request, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return original_send(request, **kwargs)

        session.send = send
        return True
    except Exception:      # noqa: BLE001 -- a missing internal is not fatal
        return False
