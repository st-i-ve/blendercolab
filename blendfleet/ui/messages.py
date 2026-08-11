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
from blendfleet.fleet import (FleetBusyError, UnreachableAccountsError,
                              WrongUsernameError)
from blendfleet.kaggle_client import KaggleError
from blendfleet.uploader import UploadError

# Exceptions raised by this codebase's own modules whose message text
# already explains what happened, why, and what to do next -- see their
# docstrings. Shown to the user directly (with the failing action
# prefixed), never re-wrapped.
_SELF_EXPLANATORY = (
    FleetBusyError, UnreachableAccountsError, WrongUsernameError, KaggleError,
    SyncError, UploadError, TokenFormatError, ValueError,
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


# ---------------------------------------------------------------------------
# Task 4: "it just says error" -- kernels_status's own failure_message is
# often empty, and even when it is not, neither it nor a raw kernel-log
# tail is something a user should have to read as a traceback. Each entry
# below is (keywords to look for, one-line cause, full what/why/next-step
# explanation) for the failures actually worth telling apart: they need
# different fixes, so a single "render failed" is not enough. Checked in
# order, case-insensitively, against whatever text is available (Kaggle's
# failure_message, or the kernel log tail fetched via
# Fleet.fetch_failure_log when that came back empty).
# ---------------------------------------------------------------------------
_FAILURE_CAUSES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    # Measured on a real render: Blender exits in about a second, per
    # frame, with "ERROR Cannot render, no camera" -- and without this the
    # app reported only that every frame failed, which looks like a fault
    # in the farm rather than a scene that cannot be rendered by anything.
    # Listed FIRST because it also matches "blender quit" below, and the
    # specific cause is far more useful than "Blender crashed".
    (("cannot render, no camera", "no camera"),
     "The scene has no camera",
     "Blender cannot render a scene with no active camera, so every frame "
     "failed within a second or so -- nothing to do with Kaggle, the GPU "
     "or the fleet. Open the .blend, add a camera (or set an existing one "
     "as the scene's active camera in Scene Properties), save, and upload "
     "the scene again."),
    (("out of memory", "outofmemory", "cuda out of memory", "memoryerror",
      "bad_alloc", "killed process", "oom-killer", "oom killer"),
     "Ran out of memory",
     "The render used more memory than the kernel had available -- either "
     "the GPU's own VRAM (a CUDA/GPU out-of-memory) or the kernel's system "
     "RAM. This almost always means the resolution, sample count, or scene "
     "complexity (dense geometry, large textures) is too much for the "
     "hardware Kaggle happened to allocate this run. Lower the resolution "
     "or sample count, or split the frame range into smaller batches, then "
     "try again."),
    (("segmentation fault", "sigsegv", "core dumped", "access violation",
      "blender quit", "fatal python error"),
     "Blender crashed",
     "Blender itself stopped unexpectedly partway through the render "
     "(a crash), rather than Kaggle stopping the session on purpose. This "
     "is usually caused by one specific frame or scene feature Blender "
     "cannot handle on this hardware -- try opening the .blend locally and "
     "rendering the same frame to reproduce it, then retry the render."),
    (("no such file or directory", "filenotfounderror", "cannot find",
      "not found:", "missing_file", "file not found"),
     "A file the render needed was missing",
     "The render could not find a file it needed. The most common cause "
     "is the .blend's own dataset share not having fully propagated to "
     "this account yet; the next most common is a linked asset (texture, "
     "library .blend) that was not packed into the file. Use Blender's "
     "File > External Data > Pack Resources before re-uploading, or simply "
     "launch again once sharing has had a little longer to propagate."),
    (("timed out", "session timeout", "exceeded the maximum", "deadline exceeded"),
     "The session timed out",
     "The kernel ran longer than Kaggle allows for a single session and "
     "was stopped before every assigned frame finished. Split the frame "
     "range into smaller batches so each account's job comfortably "
     "finishes inside one session, then try again."),
    (("quota", "usage limit", "exceeded your weekly", "no gpu quota"),
     "This account's GPU quota ran out",
     "Kaggle stopped the session because this account had no GPU quota "
     "left this week. Check the account's remaining quota under Manage "
     "accounts…, wait for Kaggle's weekly reset, or swap in a different "
     "account for this render."),
)


# Kaggle's own kernel-status vocabulary -- worth treating identically to
# "nothing to go on", because showing the literal word back to the user
# ("Failed: error") is exactly the "it just says error" complaint the whole
# task exists to fix, not an explanation.
_BARE_STATUS_WORDS = {"error", "failed", "failure"}


def explain_kernel_failure(raw: str) -> tuple[str, str]:
    """(one-line cause, full explanation) for a failed kernel.

    `raw` is whatever text is actually available: kernels_status's own
    failure_message when Kaggle supplied one, otherwise the tail of the
    kernel log (Fleet.fetch_failure_log) when it did not. Recognised
    causes are translated into plain language, following this module's own
    what/why/next-step register -- never a bare status word, never a raw
    traceback shown as if it were the explanation. Anything unrecognised
    still shows the raw detail (never hidden), just introduced honestly as
    unrecognised rather than mis-translated into the wrong category.
    """
    text = (raw or "").strip()
    if text.lower() in _BARE_STATUS_WORDS:
        text = ""
    lowered = text.lower()
    for keywords, cause, explanation in _FAILURE_CAUSES:
        if any(keyword in lowered for keyword in keywords):
            return cause, explanation
    if not text:
        return (
            "Failed for an unknown reason",
            "Kaggle reported this account's render as failed but gave no "
            "failure message, and no kernel log could be fetched either. "
            "Open the notebook directly at kaggle.com to see what "
            "happened, or just retry the render."
        )
    one_line = text.splitlines()[0].strip()[:120] or "Failed for an unknown reason"
    return (
        one_line,
        "BlendFleet does not recognise this failure pattern, so here is "
        f"the raw detail that was reported:\n\n{text}\n\n"
        "Check the notebook's own log at kaggle.com for the full picture, "
        "or retry the render."
    )
