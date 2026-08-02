"""One Kaggle account, driven through the in-process Python API.

Deliberately does NOT shell out to the `kaggle` CLI. PyInstaller bundles
Python modules but not console scripts, so a subprocess call to `kaggle`
works in development and then fails in the packaged .exe. Everything here
goes through KaggleApi / kagglesdk instead.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from requests.exceptions import HTTPError

from blendfleet.uploader import UploadError, upload_file

# Status strings returned by ApiGetKernelSessionStatusResponse.status
ACTIVE_STATES = {"queued", "running"}

# Kaggle's own dataset-metadata files -- never data to upload as a blob.
# Mirrors kaggle_api_extended.py's DATASET_METADATA_FILE/OLD_DATASET_METADATA_FILE
# (dataset-metadata.json / datapackage.json), which upload_files() skips too.
_METADATA_FILENAMES = {"dataset-metadata.json", "datapackage.json"}

# Extensions Blender can be asked to write from the dashboard's Format combo
# (PNG -> .png, JPEG -> .jpg). fetch_output() must look for all of them or a
# JPEG render silently collects zero frames.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")

ENV_TOKEN = "KAGGLE_API_TOKEN"

# Guards the ONLY remaining set-environment-then-construct sequence in the
# app (_default_api_factory). kaggle's KaggleApi has no api_token= parameter:
# authenticate() reads os.environ, and it does a network round trip
# (_introspect_token) between the read and the credential landing on the
# instance. Two threads racing through that window authenticate as each
# other. Everything else passes api_token= explicitly and never touches
# os.environ at all.
_ENV_TOKEN_LOCK = threading.Lock()


class KaggleError(Exception):
    """A Kaggle call failed."""


def _dataset_error_detail(e: HTTPError) -> str:
    """Pull the actual reason out of an HTTPError from the dataset API.

    requests' HTTPError.__str__ is just the status line ("400 Client Error:
    Bad Request for url: ..."); the useful part -- Kaggle's JSON body, e.g.
    {"error": {"message": "Please upload at least one file", ...}} -- is on
    .response and never reaches the user otherwise.
    """
    resp = getattr(e, "response", None)
    if resp is None:
        return str(e)
    try:
        body = resp.json()
        message = body.get("error", {}).get("message")
        if message:
            return message
    except (ValueError, AttributeError):
        pass
    text = getattr(resp, "text", "") or ""
    return text.strip() or str(e)


def _raise_dataset_upload_error(action: str, e: HTTPError) -> None:
    """Turn a raw dataset-upload HTTPError into an actionable KaggleError.

    A file that fails to upload (flaky/slow connection, common with a large
    .blend) does NOT raise on its own: the kaggle package's upload_files()
    exhausts its internal retry budget and silently drops the file rather
    than raising, then still submits the create/version call with no file
    attached. Kaggle correctly 400s that with "Please upload at least one
    file" -- but surfaced as a bare HTTPError this reads like a metadata or
    API-usage bug rather than what it actually is: the upload never landed.
    """
    detail = _dataset_error_detail(e)
    if "upload at least one file" in detail.lower():
        raise KaggleError(
            f"{action} failed: the .blend file did not finish uploading to "
            "Kaggle, so the request was submitted with no file attached "
            "(Kaggle said: \"" + detail + "\"). This usually means the "
            "upload was interrupted by a slow or flaky connection -- retry "
            "the render."
        ) from e
    raise KaggleError(f"{action} failed: {detail}") from e


class _RequestsPutTransport:
    """Real `blendfleet.uploader.Transport` backed by `requests`.

    Satisfies the Transport protocol's duck-typed contract exactly:
    `.put(url, data, headers)` plus a `.token` attribute. `token` is set
    from the blob-upload-session start response (see `_start_blob_upload`
    below) -- per uploader.py's own docstring, the real Kaggle blob token
    lives there, never in the GCS PUT response body, so the transport
    (which owns the session) is what has to carry it.

    The `create_url` a session hands back is a presigned GCS resumable-
    upload URL (confirmed live in docs/upload-concurrency-findings.md) --
    no Authorization header is needed or added on the PUT itself.
    """

    def __init__(self, token: str):
        self.token = token

    def put(self, url: str, data, headers: dict):
        return requests.put(url, data=data, headers=headers)


def _start_blob_upload(sdk, path: Path, blob_type) -> tuple[str, str]:
    """Open a real Kaggle/GCS resumable-upload session for `path`.

    Mirrors kaggle_api_extended.py::_upload_blob's own call to
    `kaggle.blobs.blob_api_client.start_blob_upload(ApiStartBlobUploadRequest)`
    -- that response is where the session URL (`create_url`) and the blob
    `token` actually come from. Returns (session_url, token).
    """
    from kagglesdk.blobs.types.blob_api_service import ApiStartBlobUploadRequest

    request = ApiStartBlobUploadRequest()
    request.type = blob_type
    request.name = path.name
    request.content_length = path.stat().st_size
    request.last_modified_epoch_seconds = int(path.stat().st_mtime)
    response = sdk.blobs.blob_api_client.start_blob_upload(request)
    return response.create_url, response.token


@dataclass
class KernelStatus:
    state: str          # queued|running|complete|error|cancel_requested|
                        # cancel_acknowledged|new_script|not_started
    message: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


@dataclass
class Quota:
    used_seconds: int
    total_seconds: int
    refresh_time: str
    source: str = "api"     # ALWAYS label the source: the settings page has
                            # been observed to disagree with this figure.


def _with_env_token(token: str, construct: Callable):
    """Run `construct()` with KAGGLE_API_TOKEN set to `token`, serialized.

    Last resort, used only where the library gives us no way to pass a token
    in. The lock is held across the whole set-construct-restore sequence so
    the global can never be observed by another thread holding a different
    token, and the previous value is restored afterwards so the token does
    not linger process-wide (child processes, crash dumps).
    """
    with _ENV_TOKEN_LOCK:
        previous = os.environ.get(ENV_TOKEN)
        os.environ[ENV_TOKEN] = token
        try:
            return construct()
        finally:
            if previous is None:
                os.environ.pop(ENV_TOKEN, None)
            else:
                os.environ[ENV_TOKEN] = previous


def _default_api_factory(token: str):
    """KaggleApi takes no api_token argument (checked against the installed
    kaggle package): authenticate() reads the environment. Lock-guarded.
    After authenticate() the token lives on api.config_values, so every later
    call on that instance is bound to this account regardless of the global.
    """
    from kaggle.api.kaggle_api_extended import KaggleApi

    def construct():
        api = KaggleApi()
        api.authenticate()
        return api

    return _with_env_token(token, construct)


def _default_sdk_factory(token: str):
    """Env-free: kagglesdk.KaggleClient accepts api_token= and only falls
    back to os.environ when it is None (kaggle_http_client.py:268)."""
    from kagglesdk import KaggleClient as SdkClient
    return SdkClient(api_token=token)


class KaggleClient:
    def __init__(self, token: str,
                 api_factory: Callable = _default_api_factory,
                 sdk_factory: Callable = _default_sdk_factory,
                 upload_blob_fn: Callable[[Path, Callable | None], str] | None = None
                 ) -> None:
        self.token = token
        self._api_factory = api_factory
        self._sdk_factory = sdk_factory
        # Injectable so tests never touch the network; production default
        # is the real, reliable uploader (blendfleet.uploader.upload_file)
        # instead of the kaggle package's own upload_files()/_upload_blob(),
        # which silently drops a file and returns None when its retries run
        # out (see blendfleet/uploader.py's module docstring). A test stub
        # that raises UploadError, or one that returns a falsy token, must
        # both be treated as "did not upload" by _preflight_upload below.
        self._upload_blob_fn = upload_blob_fn or self._default_upload_blob
        self._api = None

    @property
    def api(self):
        if self._api is None:
            self._api = self._api_factory(self.token)
        return self._api

    def _default_upload_blob(self, path: Path,
                              on_progress: Callable | None = None) -> str:
        """Real single-file upload: open a genuine blob-upload session
        against Kaggle, then hand the PUT to blendfleet.uploader.upload_file
        (retries with resume, raises UploadError instead of returning None).
        """
        from kagglesdk.blobs.types.blob_api_service import ApiBlobType

        sdk = self._sdk_factory(self.token)
        session_url, token = _start_blob_upload(sdk, path, ApiBlobType.DATASET)
        transport = _RequestsPutTransport(token=token)
        return upload_file(path, session_url, transport, on_progress=on_progress)

    def _files_to_upload(self, folder: Path) -> list[Path]:
        """Every real data file staged in `folder` -- everything except
        Kaggle's own dataset-metadata files, which are never blobs."""
        if not folder.exists():
            return []
        return sorted(p for p in folder.iterdir()
                      if p.is_file() and p.name not in _METADATA_FILENAMES)

    def _preflight_upload(self, folder: Path,
                          on_progress: Callable | None) -> dict[str, str]:
        """Upload every real file in `folder` reliably BEFORE any create/
        version request is ever sent to Kaggle -- the second, independent
        defence against the empty-file-list 400: even if the upload fails
        in some way that doesn't raise (a stub/future implementation that
        merely returns a falsy token), this refuses locally with a clear
        message instead of letting a create/version call go out with
        nothing attached. Returns {filename: token} so the caller can wire
        the already-uploaded tokens into kaggle's own upload path instead
        of uploading the same file a second time.
        """
        tokens: dict[str, str] = {}
        for path in self._files_to_upload(folder):
            try:
                token = self._upload_blob_fn(path, on_progress)
            except UploadError as e:
                raise KaggleError(
                    f"upload of {path.name} did not complete: {e} -- the "
                    "file was not fully uploaded, so nothing was submitted "
                    "to Kaggle. This usually means a slow or flaky "
                    "connection; retry the render."
                ) from e
            if not token:
                raise KaggleError(
                    f"upload of {path.name} did not complete: no blob token "
                    "was returned, so nothing was submitted to Kaggle. "
                    "Retry the render."
                )
            tokens[path.name] = token
        return tokens

    def _patch_upload_blob(self, tokens: dict[str, str],
                           on_progress: Callable | None) -> None:
        """Replace the real KaggleApi's private `_upload_blob` with one that
        returns the token we already obtained in `_preflight_upload`,
        instead of letting kaggle's own dataset_create_new/dataset_create_
        version re-upload the same file a second time over the network via
        its flaky one-shot-retry path. A no-op on the fake API used by
        tests (which never calls `_upload_blob` at all), and safe for any
        file NOT already preflighted (falls back to the real reliable
        uploader rather than kaggle's flaky one).
        """
        def reliable_upload_blob(full_path, quiet, blob_type, upload_context,
                                 content_type=None):
            name = Path(full_path).name
            if name in tokens:
                return tokens[name]
            return self._upload_blob_fn(Path(full_path), on_progress)

        self.api._upload_blob = reliable_upload_blob

    # ---------------- identity ----------------
    def whoami(self) -> str:
        kernels = self.api.kernels_list(mine=True, page_size=1)
        for k in kernels:
            ref = getattr(k, "ref", "")
            if "/" in ref:
                return ref.split("/", 1)[0]
        raise KaggleError(
            "could not determine username: this account has no notebooks. "
            "Create one on kaggle.com first, or set the username manually.")

    # ---------------- quota ----------------
    def quota(self) -> Quota:
        from kagglesdk.kernels.types.kernels_api_service import (
            ApiGetAcceleratorQuotaStatisticsRequest)
        sdk = self._sdk_factory(self.token)
        r = sdk.kernels.kernels_api_client.get_accelerator_quota_statistics(
            ApiGetAcceleratorQuotaStatisticsRequest())
        return Quota(
            used_seconds=int(r.gpu_quota.time_used.total_seconds()),
            total_seconds=int(r.gpu_quota.total_time_allowed.total_seconds()),
            refresh_time=str(r.quota_refresh_time),
            source="api")

    # ---------------- datasets ----------------
    def dataset_exists(self, slug: str) -> bool:
        """A missing or invisible dataset raises HTTPError 403, not 404 --
        Kaggle does not reveal whether a private dataset exists. Any failure
        is therefore treated as 'not usable by us', which is what callers mean.
        """
        try:
            self.api.dataset_status(slug)
            return True
        except Exception:
            return False

    def dataset_create(self, folder: Path,
                       on_progress: Callable | None = None) -> None:
        folder = Path(folder)
        tokens = self._preflight_upload(folder, on_progress)
        self._patch_upload_blob(tokens, on_progress)
        try:
            self.api.dataset_create_new(folder=str(folder), dir_mode="skip",
                                        convert_to_csv=False, public=False)
        except HTTPError as e:
            _raise_dataset_upload_error("Dataset creation", e)

    def dataset_version(self, folder: Path, message: str,
                        on_progress: Callable | None = None) -> None:
        folder = Path(folder)
        tokens = self._preflight_upload(folder, on_progress)
        self._patch_upload_blob(tokens, on_progress)
        try:
            self.api.dataset_create_version(folder=str(folder),
                                            version_notes=message,
                                            dir_mode="skip", convert_to_csv=False,
                                            delete_old_versions=False)
        except HTTPError as e:
            _raise_dataset_upload_error("Dataset versioning", e)

    # ---------------- kernels ----------------
    def push_kernel(self, folder: Path) -> None:
        """Always starts a run -- there is no unchanged-content short circuit."""
        self.api.kernels_push(folder=str(folder))

    def status(self, slug: str) -> KernelStatus:
        """A kernel with no session raises ValueError ("Cannot access kernel"),
        which means 'not started yet', not a failure."""
        try:
            r = self.api.kernels_status(slug)
        except ValueError:
            return KernelStatus(state="not_started")
        except Exception as e:
            raise KaggleError(str(e)) from e
        # r.status may be an enum whose str() is "KernelWorkerStatus.COMPLETE",
        # or a plain string "COMPLETE". Normalise both to "complete".
        raw = str(getattr(r, "status", ""))
        state = raw.rsplit(".", 1)[-1].lower()
        return KernelStatus(state=state,
                            message=getattr(r, "failure_message", None) or "")

    def cancel(self, slug: str) -> bool:
        """The CLI has no cancel subcommand, which is why this is widely
        believed impossible. The SDK exposes the RPC.

        Returns False rather than raising, but the caller MUST report that:
        a cancel that quietly failed leaves somebody else's GPU quota
        draining. fleet.cancel_all() turns this into a per-account result.
        """
        try:
            from kagglesdk.kernels.types.kernels_api_service import (
                ApiCancelKernelSessionRequest)
            owner, name = slug.split("/", 1)
            req = ApiCancelKernelSessionRequest()
            req.user_name, req.kernel_slug = owner, name
            self._sdk_factory(self.token).kernels.kernels_api_client \
                .cancel_kernel_session(req)
            return True
        except Exception:
            return False

    def fetch_output(self, slug: str, dest: Path) -> list[Path]:
        """Return every rendered image, whatever format was selected.

        Globbing *.png only made the dashboard's JPEG option a dead setting:
        Blender wrote .jpg and collect() then reported every frame missing.
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        self.api.kernels_output(slug, path=str(dest))
        return sorted(p for p in dest.rglob("*")
                      if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def verify_token(token: str,
                  client_factory: Callable[[str], "KaggleClient"] | None = None
                  ) -> str | None:
    """Confirm `token` actually works against Kaggle, for
    accounts.AccountStore.add()/reverify() to use as a `verifier`.

    Returns the resolved username on success. Returns None -- NOT a failure
    -- when the token is valid but whoami() cannot resolve a handle because
    the account has never created a notebook (see whoami()'s docstring);
    that account is still accepted, just with an unresolved username. Any
    other problem (revoked token, bad format, network down) propagates so
    the caller can show the real error and refuse to add/re-verify.
    """
    factory = client_factory or KaggleClient
    client = factory(token)
    try:
        return client.whoami()
    except KaggleError as e:
        if "no notebooks" in str(e).lower():
            return None
        raise
