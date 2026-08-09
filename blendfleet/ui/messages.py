"""Turn an exception into a message a person can act on.

The rule for every user-facing string in this app (see the task brief):
state what happened, why, and what to do next. Never a bare status code,
never a raw Python exception.

Several of this codebase's own exception types already do that in full --
FleetBusyError, UnreachableAccountsError, KaggleError, SyncError,
UploadError, TokenFormatError all carry a complete explanation in their
message (see their docstrings in fleet.py/kaggle_client.py/dataset_sync.py/
uploader.py/accounts.py) -- so those are shown close to verbatim, just
prefixed with the action that failed. Anything else (a bare OSError, an
unexpected AttributeError from a library internals change, etc.) gets
wrapped in the same what/why/next-step shape instead of leaking straight
through to a QMessageBox.
"""
from __future__ import annotations

from blendfleet.accounts import TokenFormatError
from blendfleet.dataset_sync import SyncError
from blendfleet.fleet import FleetBusyError, UnreachableAccountsError
from blendfleet.kaggle_client import KaggleError
from blendfleet.uploader import UploadError

# Exceptions raised by this codebase's own modules whose message text
# already explains what happened, why, and what to do next -- see their
# docstrings. Shown to the user directly (with the failing action
# prefixed), never re-wrapped.
_SELF_EXPLANATORY = (
    FleetBusyError, UnreachableAccountsError, KaggleError, SyncError,
    UploadError, TokenFormatError, ValueError,
)


def explain(action: str, exc: Exception) -> str:
    """Render `exc`, raised while doing `action`, as a full sentence a
    user can act on.

    `action` should read as a gerund/verb phrase, e.g. "Starting the
    render", "Cancelling the render", "Collecting frames" -- it is
    prefixed onto the explanation so the message names what was being
    attempted, not just what broke.
    """
    text = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, _SELF_EXPLANATORY):
        return f"{action} failed: {text}"
    return (
        f"{action} failed: {text}\n\n"
        "This was not one of the problems BlendFleet knows how to explain "
        "in detail. Check your internet connection and try again; if it "
        "keeps happening, re-verify your accounts under Manage accounts…"
    )
