import datetime as _dt
import json
from pathlib import Path

import pytest
from requests.exceptions import HTTPError

from blendfleet.kaggle_client import KaggleClient, KaggleError, verify_token

TOKEN = "KGAT_" + "a" * 32


class FakeResponse:
    """Stands in for requests.Response: only .json()/.text are used."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def make_http_error(status_code: int, message: str) -> HTTPError:
    """Build an HTTPError shaped like the real Kaggle API's 400 responses:
    {"error": {"code": ..., "message": ..., "status": "INVALID_ARGUMENT"}}."""
    resp = FakeResponse(status_code, {
        "error": {"code": status_code, "message": message,
                  "status": "INVALID_ARGUMENT"}})
    e = HTTPError(f"{status_code} Client Error: Bad Request for url: "
                  "https://api.kaggle.com/v1/datasets.DatasetApiService/"
                  "CreateDatasetVersion")
    e.response = resp
    return e


class FakeKernel:
    def __init__(self, ref):
        self.ref = ref


class FakeStatus:
    def __init__(self, status, failure_message=None):
        self.status = status
        self.failure_message = failure_message


class FakeApi:
    """Stands in for KaggleApi. Raises what the real API actually raises."""

    def __init__(self, status="COMPLETE", dataset_ok=True, kernels=None,
                 create_error=None, version_error=None, list_files_ok=True):
        self._status = status
        self._dataset_ok = dataset_ok
        self._list_files_ok = list_files_ok
        self._kernels = kernels if kernels is not None else [
            FakeKernel("stivestivewithani/remember-render")]
        self._create_error = create_error
        self._version_error = version_error
        self.pushed = []
        self.created = []
        self.versioned = []

    def kernels_list(self, mine=False, page_size=1):
        return self._kernels

    def kernels_status(self, slug):
        if self._status == "MISSING":
            raise ValueError(f"Cannot access kernel '{slug}' (Permission ...)")
        if self._status == "BOOM":
            raise RuntimeError("connection reset")
        return FakeStatus(self._status)

    def kernels_push(self, folder):
        self.pushed.append(folder)

    def kernels_output(self, slug, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "f_0001.png").write_bytes(b"PNG")

    def dataset_status(self, slug):
        if not self._dataset_ok:
            # the real API raises HTTPError 403 here, never a 404
            raise RuntimeError("403 Client Error: Forbidden for url: ...")
        return "ready"

    def dataset_list_files(self, slug):
        if not self._list_files_ok:
            raise RuntimeError("403 Client Error: Forbidden for url: ...")
        return {"datasetFiles": []}

    def dataset_create_new(self, folder, **kw):
        if self._create_error is not None:
            raise self._create_error
        self.created.append((folder, kw))

    def dataset_create_version(self, folder, version_notes, **kw):
        if self._version_error is not None:
            raise self._version_error
        self.versioned.append((folder, version_notes, kw))


def client(api=None, **kw):
    api = api or FakeApi(**kw)
    return KaggleClient(TOKEN, api_factory=lambda t: api), api


def test_does_not_shell_out_to_the_kaggle_cli():
    # PyInstaller bundles modules, not console scripts: a subprocess call to
    # `kaggle` would work in dev and then fail in the packaged .exe.
    src = Path("blendfleet/kaggle_client.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src
    assert "subprocess.run" not in src


def test_whoami_extracts_owner_from_ref():
    c, _ = client()
    assert c.whoami() == "stivestivewithani"


def test_whoami_raises_helpfully_when_account_has_no_notebooks():
    c, _ = client(kernels=[])
    with pytest.raises(KaggleError, match="no notebooks"):
        c.whoami()


def test_status_is_structured_not_parsed_from_prose():
    c, _ = client(status="COMPLETE")
    assert c.status("x/y").state == "complete"


def test_status_strips_enum_prefix():
    # the live API returns an enum whose str() is "KernelWorkerStatus.COMPLETE"
    class EnumLike:
        def __str__(self):
            return "KernelWorkerStatus.COMPLETE"

    class Api(FakeApi):
        def kernels_status(self, slug):
            return FakeStatus(EnumLike())

    c, _ = client(api=Api())
    assert c.status("x/y").state == "complete"


def test_status_running_is_active():
    c, _ = client(status="RUNNING")
    st = c.status("x/y")
    assert st.state == "running" and st.is_active


def test_missing_kernel_means_not_started_not_error():
    # the real API raises ValueError("Cannot access kernel ..."), not a 404
    c, _ = client(status="MISSING")
    st = c.status("x/y")
    assert st.state == "not_started"
    assert not st.is_active


def test_unexpected_failure_still_raises():
    c, _ = client(status="BOOM")
    with pytest.raises(KaggleError, match="connection reset"):
        c.status("x/y")


def test_dataset_exists_true_when_status_returns():
    c, _ = client(dataset_ok=True)
    assert c.dataset_exists("me/x") is True


def test_dataset_exists_false_on_403_not_404():
    c, _ = client(dataset_ok=False)
    assert c.dataset_exists("me/x") is False


# ------------------------------------------- dataset_reachable (Task 3) --
# Live check (task-3-report.md) found dataset_status() 404s for a
# non-owner account even with a genuine READER grant -- it only reflects
# datasets the calling account owns. dataset_list_files() is the one that
# is actually gated on real read access. dataset_reachable() must use
# THAT, and must disagree with dataset_exists() in exactly the scenario
# that was measured live: dataset_status() failing while the account can
# really read the dataset.

def test_dataset_reachable_true_when_list_files_succeeds():
    c, _ = client(list_files_ok=True)
    assert c.dataset_reachable("owner/x") is True


def test_dataset_reachable_false_when_list_files_forbidden():
    c, _ = client(list_files_ok=False)
    assert c.dataset_reachable("owner/x") is False


def test_dataset_reachable_disagrees_with_dataset_exists_for_a_shared_dataset():
    """Reproduces the live finding: dataset_status() (dataset_exists) 404s
    for a friend with a real grant, while dataset_list_files()
    (dataset_reachable) correctly reflects that the grant works."""
    c, _ = client(dataset_ok=False, list_files_ok=True)
    assert c.dataset_exists("owner/x") is False, \
        "dataset_status is owner-only -- must still fail here"
    assert c.dataset_reachable("owner/x") is True, \
        "dataset_list_files must correctly show the real grant works"


def test_dataset_create_passes_skip_dir_mode_and_private(tmp_path):
    c, api = client()
    c.dataset_create(tmp_path)
    # dataset_create_new captures the **kw dict; assert the safety-critical flags
    assert len(api.created) == 1
    folder, kw = api.created[0]
    assert kw["dir_mode"] == "skip", "dir_mode zip nests payload, breaks /kaggle/input"
    assert kw["public"] is False, "datasets must never be public by default"


def test_dataset_version_passes_skip_dir_mode(tmp_path):
    c, api = client()
    c.dataset_version(tmp_path, "my message")
    # dataset_create_version captures the **kw dict; assert the safety-critical flag
    assert len(api.versioned) == 1
    folder, version_notes, kw = api.versioned[0]
    assert kw["dir_mode"] == "skip", "dir_mode zip nests payload, breaks /kaggle/input"
    assert version_notes == "my message"


def test_dataset_version_no_file_400_raises_actionable_kaggle_error(tmp_path):
    """Reproduces the real bug: kaggle's upload_files() exhausts its retry
    budget on a file that fails to upload and silently proceeds without it,
    so Kaggle 400s CreateDatasetVersion with "Please upload at least one
    file". A bare HTTPError reads like a metadata bug; this must surface as
    an actionable message about the upload, not the raw status line."""
    error = make_http_error(400, "Please upload at least one file")
    c, api = client(version_error=error)
    with pytest.raises(KaggleError) as exc_info:
        c.dataset_version(tmp_path, "update x.blend")
    message = str(exc_info.value)
    assert "did not finish uploading" in message
    assert "retry the render" in message.lower()
    assert "Please upload at least one file" in message
    # the original HTTPError must still be reachable for diagnostics
    assert isinstance(exc_info.value.__cause__, HTTPError)


def test_dataset_create_no_file_400_raises_actionable_kaggle_error(tmp_path):
    error = make_http_error(400, "Please upload at least one file")
    c, api = client(create_error=error)
    with pytest.raises(KaggleError) as exc_info:
        c.dataset_create(tmp_path)
    assert "did not finish uploading" in str(exc_info.value)


def test_dataset_version_other_400_surfaces_real_message(tmp_path):
    """A different 400 must not be mislabeled as an upload failure -- the
    actual Kaggle message must reach the caller instead of the generic
    status line OR the unrelated upload-failure hint."""
    error = make_http_error(400, "Invalid dataset slug")
    c, api = client(version_error=error)
    with pytest.raises(KaggleError) as exc_info:
        c.dataset_version(tmp_path, "update x.blend")
    message = str(exc_info.value)
    assert "Invalid dataset slug" in message
    assert "did not finish uploading" not in message


# ------------------------------------------------------- upload preflight --
# Task 2: use blendfleet/uploader.py instead of relying on kaggle's own
# upload_files()/_upload_blob(), which silently drops a file and returns
# None when its retries run out (see blendfleet/uploader.py's docstring).
# These stub the injected upload_blob_fn (never touching the network) to
# reproduce both observed failure shapes -- raising, and returning a falsy
# token -- and assert the real Kaggle create/version API method is NEVER
# reached in either case: the 400 from an empty file list must never even
# be possible, because the request is never sent.

def _staged_folder(tmp_path, filename="big.blend", size=500):
    folder = tmp_path / "stage"
    folder.mkdir()
    (folder / filename).write_bytes(b"B" * size)
    (folder / "dataset-metadata.json").write_text("{}")
    return folder


def test_upload_raises_refuses_locally_and_never_calls_dataset_create_version(tmp_path):
    folder = _staged_folder(tmp_path)

    def failing_upload(path, on_progress):
        from blendfleet.uploader import UploadError
        raise UploadError("upload failed after 6 retries", status=503, body="unavailable")

    c, api = client()
    c._upload_blob_fn = failing_upload

    with pytest.raises(KaggleError) as exc_info:
        c.dataset_version(folder, "update big.blend")

    message = str(exc_info.value)
    assert "did not complete" in message
    assert "retry the render" in message.lower()
    # the real Kaggle API method must NEVER have been invoked
    assert api.versioned == []


def test_upload_yields_no_token_refuses_locally_and_never_calls_dataset_create_version(tmp_path):
    """Even a hypothetical upload path that fails "some other way" -- by
    returning a falsy token instead of raising -- must still be caught
    locally, never handed to Kaggle as if it were a real upload."""
    folder = _staged_folder(tmp_path)

    def no_token_upload(path, on_progress):
        return None

    c, api = client()
    c._upload_blob_fn = no_token_upload

    with pytest.raises(KaggleError, match="no blob token"):
        c.dataset_create(folder)

    assert api.created == []


def test_upload_success_proceeds_to_dataset_create_version(tmp_path):
    folder = _staged_folder(tmp_path)
    calls = []

    def fake_upload(path, on_progress):
        calls.append(path.name)
        return "tok-123"

    c, api = client()
    c._upload_blob_fn = fake_upload

    c.dataset_version(folder, "update big.blend")

    assert calls == ["big.blend"], "only the real data file, never the metadata json"
    assert len(api.versioned) == 1


def test_on_progress_threaded_through_to_the_uploader(tmp_path):
    folder = _staged_folder(tmp_path)
    seen_progress = []

    def fake_upload(path, on_progress):
        seen_progress.append(on_progress)
        return "tok-abc"

    c, api = client()
    c._upload_blob_fn = fake_upload
    marker = object()

    c.dataset_create(folder, on_progress=marker)

    assert seen_progress == [marker]


def test_preflighted_file_is_not_uploaded_a_second_time_via_the_patched_api(tmp_path):
    """Once dataset_version has reliably uploaded the file itself, kaggle's
    own (real) _upload_blob must not re-upload the same file again over the
    network -- the patched method should just hand back the token already
    obtained. This is the mechanism that avoids doubling upload time/bytes
    for a large .blend."""
    folder = _staged_folder(tmp_path)
    call_count = {"n": 0}

    def fake_upload(path, on_progress):
        call_count["n"] += 1
        return f"tok-{call_count['n']}"

    c, api = client()
    c._upload_blob_fn = fake_upload

    c.dataset_version(folder, "update big.blend")
    assert call_count["n"] == 1

    # Simulate what kaggle's own dataset_create_version would do internally
    # (kaggle_api_extended.py's upload_files() -> _upload_file() ->
    # self._upload_blob(full_path, quiet, blob_type, upload_context)).
    full_path = str(folder / "big.blend")
    token = api._upload_blob(full_path, True, None, None)
    assert token == "tok-1"
    assert call_count["n"] == 1, "must reuse the cached token, not upload again"


def test_push_never_treated_as_noop():
    # kernels push ALWAYS starts a run; no unchanged-content short circuit
    c, api = client()
    c.push_kernel(Path("/tmp/x"))
    c.push_kernel(Path("/tmp/x"))
    assert len(api.pushed) == 2


def test_quota_converts_seconds_and_labels_source():
    class FakeQuotaResp:
        class gpu_quota:
            time_used = _dt.timedelta(seconds=3600)
            total_time_allowed = _dt.timedelta(seconds=21600)
        quota_refresh_time = "2026-08-01T00:00:00Z"

    class FakeSdk:
        class kernels:
            class kernels_api_client:
                @staticmethod
                def get_accelerator_quota_statistics(req):
                    return FakeQuotaResp()

    c = KaggleClient(TOKEN, api_factory=lambda t: FakeApi(),
                     sdk_factory=lambda t: FakeSdk())
    q = c.quota()
    assert q.used_seconds == 3600
    assert q.total_seconds == 21600
    # must be labelled: the settings page has disagreed with this figure
    assert q.source == "api"


def test_fetch_output_returns_pngs(tmp_path):
    c, _ = client()
    got = c.fetch_output("x/y", tmp_path / "out")
    assert [p.name for p in got] == ["f_0001.png"]


def test_verify_token_returns_username_on_success():
    got = verify_token(TOKEN, client_factory=lambda t: client()[0])
    assert got == "stivestivewithani"


def test_verify_token_returns_none_when_no_notebooks():
    """whoami() raises KaggleError('...no notebooks...') for a token that is
    perfectly valid but belongs to an account with no notebooks yet. That
    must surface as an accepted-but-unresolved None, not an exception."""
    c, _ = client(kernels=[])
    got = verify_token(TOKEN, client_factory=lambda t: c)
    assert got is None


def test_verify_token_propagates_other_kaggle_errors():
    class RevokedApi(FakeApi):
        def kernels_list(self, mine=False, page_size=1):
            raise RuntimeError("401 Client Error: Unauthorized")

    revoked, _ = client(api=RevokedApi())
    with pytest.raises(RuntimeError, match="Unauthorized"):
        verify_token(TOKEN, client_factory=lambda t: revoked)


def test_fetch_output_returns_jpegs_too(tmp_path):
    """The dashboard offers JPEG; globbing *.png only made that a dead
    option that quietly collected zero frames."""
    class JpegApi(FakeApi):
        def kernels_output(self, slug, path):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "f_0001.jpg").write_bytes(b"JPG")
            (Path(path) / "f_0002.jpeg").write_bytes(b"JPG")
            (Path(path) / "notes.txt").write_bytes(b"ignore me")

    c, _ = client(api=JpegApi())
    got = c.fetch_output("x/y", tmp_path / "out")
    assert [p.name for p in got] == ["f_0001.jpg", "f_0002.jpeg"]


def test_sdk_factory_passes_the_token_instead_of_setting_the_environment():
    """CRITICAL C2: kagglesdk.KaggleClient accepts api_token=, so nothing
    here may write the process-global KAGGLE_API_TOKEN."""
    import os
    import kagglesdk
    from blendfleet.kaggle_client import ENV_TOKEN, _default_sdk_factory

    seen = {}

    class Recorder:
        def __init__(self, api_token=None, **kw):
            seen["api_token"] = api_token
            seen["env"] = os.environ.get(ENV_TOKEN)

    original = kagglesdk.KaggleClient
    kagglesdk.KaggleClient = Recorder
    prior = os.environ.pop(ENV_TOKEN, None)
    try:
        _default_sdk_factory(TOKEN)
    finally:
        kagglesdk.KaggleClient = original
        if prior is not None:
            os.environ[ENV_TOKEN] = prior

    assert seen["api_token"] == TOKEN
    assert seen["env"] is None


# ------------------------------------------ identity binding (CRITICAL) --
# KaggleApi.authenticate() is a CASCADE (kaggle_api_extended.py:1226-1252):
# access token, then LEGACY API KEY, then OAuth, then anonymous. When the
# token we exported is revoked/mistyped, _authenticate_with_access_token's
# _introspect_token returns falsy and authenticate() drops silently through
# to whatever is in this machine's own ~/.kaggle/kaggle.json.
#
# The account then authenticates as YOU: whoami returns your handle, the
# account is stored verified=True under the wrong username, and
# fleet.dataset_reachable() passes trivially because you can always read
# your own dataset -- so UnreachableAccountsError never fires and the user
# believes three friends are contributing while all three kernels burn
# their own quota. These tests pin the check that makes that impossible.

def _install_fake_kaggle_package(monkeypatch, api_instance):
    """Put a fake `kaggle.api.kaggle_api_extended` in sys.modules.

    Importing the real one is not an option here: kaggle/__init__.py
    constructs a KaggleApi and calls authenticate() at import time, which
    reads the developer's own ~/.kaggle/kaggle.json -- the very credential
    whose leakage these tests are about.
    """
    import sys
    import types

    module = types.ModuleType("kaggle.api.kaggle_api_extended")
    module.KaggleApi = lambda: api_instance
    monkeypatch.setitem(sys.modules, "kaggle", types.ModuleType("kaggle"))
    monkeypatch.setitem(sys.modules, "kaggle.api", types.ModuleType("kaggle.api"))
    monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", module)


class BoundToOtherTokenApi:
    """A KaggleApi whose authenticate() binds a DIFFERENT token than the
    one it was given -- the shape of a session that signed in as somebody
    else."""

    def __init__(self, binds: str):
        self._binds = binds
        self.config_values: dict = {}

    def authenticate(self) -> None:
        self.config_values = {"token": self._binds,
                              "username": "somebody-else",
                              "auth_method": "AuthMethod.ACCESS_TOKEN"}


class LegacyApiKeyFallthroughApi:
    """Exactly what the real cascade produces when the access token is
    rejected: no `token` key at all, just the username/key pair read out of
    the local ~/.kaggle/kaggle.json."""

    def __init__(self, local_username: str = "the-owner-of-this-laptop"):
        self._local_username = local_username
        self.config_values: dict = {}

    def authenticate(self) -> None:
        self.config_values = {"username": self._local_username,
                              "key": "0123456789abcdef",
                              "auth_method": "AuthMethod.LEGACY_API_KEY"}


class GoodApi:
    def __init__(self, token: str):
        self._token = token
        self.config_values: dict = {}

    def authenticate(self) -> None:
        self.config_values = {"token": self._token,
                              "username": "the-real-owner",
                              "auth_method": "AuthMethod.ACCESS_TOKEN"}


def test_api_factory_rejects_a_session_bound_to_a_different_token(monkeypatch):
    from blendfleet.kaggle_client import _default_api_factory

    other = "KGAT_" + "b" * 32
    _install_fake_kaggle_package(monkeypatch, BoundToOtherTokenApi(binds=other))

    with pytest.raises(KaggleError) as exc_info:
        _default_api_factory(TOKEN, account="ada")

    message = str(exc_info.value)
    assert "ada" in message, "the message must name the account that failed"
    assert "DIFFERENT account" in message
    assert other not in message, "another account's token must never be echoed"


def test_api_factory_rejects_the_legacy_apikey_fallthrough(monkeypatch):
    """The actual production failure: a revoked friend token silently
    authenticating as the machine's own kaggle.json credentials."""
    from blendfleet.kaggle_client import _default_api_factory

    _install_fake_kaggle_package(monkeypatch, LegacyApiKeyFallthroughApi())

    with pytest.raises(KaggleError) as exc_info:
        _default_api_factory(TOKEN, account="ada")

    message = str(exc_info.value)
    assert "was not accepted by Kaggle" in message
    assert "fresh token" in message, "must say what the user should do next"


def test_api_factory_accepts_a_session_actually_bound_to_our_token(monkeypatch):
    from blendfleet.kaggle_client import _default_api_factory

    api = GoodApi(TOKEN)
    _install_fake_kaggle_package(monkeypatch, api)

    assert _default_api_factory(TOKEN, account="ada") is api


def test_api_factory_fails_closed_when_credentials_cannot_be_inspected(monkeypatch):
    """If a future kaggle release moves config_values, this must REFUSE,
    not wave the session through unverified -- an unverifiable identity is
    the exact condition this check exists to catch."""
    from blendfleet.kaggle_client import _default_api_factory

    class OpaqueApi:
        def authenticate(self) -> None:
            pass

    _install_fake_kaggle_package(monkeypatch, OpaqueApi())

    with pytest.raises(KaggleError, match="could not confirm which Kaggle account"):
        _default_api_factory(TOKEN, account="ada")


def test_api_factory_masks_the_token_when_no_account_label_is_known(monkeypatch):
    """verify_token runs before an account exists, so there is no label --
    the message still has to identify which token failed without printing
    the whole secret."""
    from blendfleet.kaggle_client import _default_api_factory

    _install_fake_kaggle_package(monkeypatch, LegacyApiKeyFallthroughApi())

    with pytest.raises(KaggleError) as exc_info:
        _default_api_factory(TOKEN)

    message = str(exc_info.value)
    assert TOKEN not in message, "the full token must never reach a dialog"
    assert TOKEN[:9] in message, "but enough of it to tell tokens apart"


def test_client_passes_its_label_to_the_identity_check(monkeypatch):
    """KaggleClient(label=...) is the whole reason the error can say
    'james' instead of a masked token."""
    _install_fake_kaggle_package(monkeypatch, LegacyApiKeyFallthroughApi())

    c = KaggleClient(TOKEN, label="james")
    with pytest.raises(KaggleError, match="'james'"):
        _ = c.api


def test_verify_token_refuses_an_account_that_authenticated_as_someone_else(monkeypatch):
    """End to end: this is what stops a revoked friend token being stored
    verified=True under the developer's own username."""
    _install_fake_kaggle_package(monkeypatch, LegacyApiKeyFallthroughApi())

    with pytest.raises(KaggleError, match="was not accepted by Kaggle"):
        verify_token(TOKEN)
