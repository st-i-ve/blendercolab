"""One Kaggle account, driven through the in-process Python API.

Deliberately does NOT shell out to the `kaggle` CLI. PyInstaller bundles
Python modules but not console scripts, so a subprocess call to `kaggle`
works in development and then fails in the packaged .exe. Everything here
goes through KaggleApi / kagglesdk instead.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Status strings returned by ApiGetKernelSessionStatusResponse.status
ACTIVE_STATES = {"queued", "running"}


class KaggleError(Exception):
    """A Kaggle call failed."""


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


def _default_api_factory(token: str):
    os.environ["KAGGLE_API_TOKEN"] = token
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    return api


def _default_sdk_factory(token: str):
    os.environ["KAGGLE_API_TOKEN"] = token
    from kagglesdk import KaggleClient as SdkClient
    return SdkClient()


class KaggleClient:
    def __init__(self, token: str,
                 api_factory: Callable = _default_api_factory,
                 sdk_factory: Callable = _default_sdk_factory) -> None:
        self.token = token
        self._api_factory = api_factory
        self._sdk_factory = sdk_factory
        self._api = None

    @property
    def api(self):
        if self._api is None:
            self._api = self._api_factory(self.token)
        return self._api

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

    def dataset_create(self, folder: Path) -> None:
        self.api.dataset_create_new(folder=str(folder), dir_mode="skip",
                                    convert_to_csv=False, public=False)

    def dataset_version(self, folder: Path, message: str) -> None:
        self.api.dataset_create_version(folder=str(folder),
                                        version_notes=message,
                                        dir_mode="skip", convert_to_csv=False,
                                        delete_old_versions=False)

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
        believed impossible. The SDK exposes the RPC."""
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
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        self.api.kernels_output(slug, path=str(dest))
        return sorted(dest.rglob("*.png"))
