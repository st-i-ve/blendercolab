import datetime as _dt
import json
from pathlib import Path

import pytest
from requests.exceptions import HTTPError

from blendfleet.kaggle_client import KaggleClient, KaggleError

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
                 create_error=None, version_error=None):
        self._status = status
        self._dataset_ok = dataset_ok
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
