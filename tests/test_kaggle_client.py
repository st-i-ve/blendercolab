import datetime as _dt
import json
import re
from pathlib import Path

import pytest
from requests.exceptions import HTTPError

from blendfleet.kaggle_client import (DatasetInfo, KaggleClient, KaggleError,
                                      RevokedTokenError, verify_token)

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


class FakeDatasetFile:
    """Stands in for kagglesdk's ApiDatasetFile: only .name/.total_bytes."""

    def __init__(self, name, total_bytes):
        self.name = name
        self.total_bytes = total_bytes


class FakeListFilesResponse:
    """Stands in for kagglesdk's ApiListDatasetFilesResponse."""

    def __init__(self, files):
        self.dataset_files = files


class FakeDataset:
    """Stands in for one dataset_list() item -- only the fields DatasetInfo
    (Task 8) actually reads: ref, title, total_bytes, last_updated,
    is_private, owner_ref (confirmed live against the installed kagglesdk,
    2026-08-12, a real account holding 8 datasets)."""

    def __init__(self, ref, title, total_bytes, last_updated, is_private,
                owner_ref):
        self.ref = ref
        self.title = title
        self.total_bytes = total_bytes
        self.last_updated = last_updated
        self.is_private = is_private
        self.owner_ref = owner_ref


class FakeApi:
    """Stands in for KaggleApi. Raises what the real API actually raises."""

    def __init__(self, status="COMPLETE", dataset_ok=True, kernels=None,
                 create_error=None, version_error=None, list_files_ok=True,
                 list_files_response=None, datasets=None,
                 dataset_delete_error=None, dataset_delete_result=True):
        self._status = status
        self._dataset_ok = dataset_ok
        self._list_files_ok = list_files_ok
        self._list_files_response = list_files_response
        self._kernels = kernels if kernels is not None else [
            FakeKernel("stivestivewithani/remember-render")]
        self._create_error = create_error
        self._version_error = version_error
        self._datasets = datasets if datasets is not None else []
        self._dataset_delete_error = dataset_delete_error
        # dataset_delete's own docstring: "Returns True if deleted, False
        # if cancelled." Defaults to True (a normal successful delete);
        # tests override this to reproduce the falsy-but-no-exception case.
        self._dataset_delete_result = dataset_delete_result
        self.pushed = []
        self.created = []
        self.versioned = []
        # Every call list_datasets() actually made -- lets a test assert on
        # what was ASKED for (user=... not mine=True), not merely on what
        # came back.
        self.dataset_list_calls: list[dict] = []
        self.dataset_delete_calls: list[tuple] = []
        # Every call this fake's kernels_output() received, in order --
        # lets a test assert on what fetch_log_tail actually ASKED for
        # (the file_pattern), not merely on the text that came back.
        self.kernels_output_calls: list[str | None] = []
        # Filenames this fake actually "downloaded" (subject to
        # file_pattern, exactly like the real kaggle package -- see
        # kaggle_api_extended.py's own kernels_output: `if compiled_pattern
        # and not compiled_pattern.search(item.file_name): continue`).
        self.kernels_output_written: list[str] = []

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

    def kernels_output(self, slug, path, file_pattern=None):
        self.kernels_output_calls.append(file_pattern)
        Path(path).mkdir(parents=True, exist_ok=True)
        self._maybe_write(path, "f_0001.png", b"PNG", file_pattern)

    def _maybe_write(self, path, name, content, file_pattern):
        # Mirrors the real API's own filtering exactly (re.search against
        # the pattern, kaggle_api_extended.py's kernels_output) so a test
        # asserting "this file was not downloaded" reflects real behaviour,
        # not just this stub's own invented shortcut.
        if file_pattern is not None and not re.search(file_pattern, name):
            return
        (Path(path) / name).write_bytes(content)
        self.kernels_output_written.append(name)

    def dataset_status(self, slug):
        if not self._dataset_ok:
            # the real API raises HTTPError 403 here, never a 404
            raise RuntimeError("403 Client Error: Forbidden for url: ...")
        return "ready"

    def dataset_list_files(self, slug):
        if not self._list_files_ok:
            raise RuntimeError("403 Client Error: Forbidden for url: ...")
        if self._list_files_response is not None:
            return self._list_files_response
        return {"datasetFiles": []}

    def dataset_create_new(self, folder, **kw):
        if self._create_error is not None:
            raise self._create_error
        self.created.append((folder, kw))

    def dataset_create_version(self, folder, version_notes, **kw):
        if self._version_error is not None:
            raise self._version_error
        self.versioned.append((folder, version_notes, kw))

    def dataset_list(self, sort_by=None, size=None, file_type=None,
                     license_name=None, tag_ids=None, search=None,
                     user=None, mine=False, page=1, max_size=None,
                     min_size=None):
        # Mirrors the real KaggleApi.dataset_list signature EXACTLY
        # (confirmed live, 2026-08-12) -- deliberately no page_size
        # parameter, so a regression that reintroduces it fails this fake
        # with a TypeError exactly like the real one does.
        self.dataset_list_calls.append({"user": user, "mine": mine})
        return self._datasets

    def dataset_delete(self, owner_slug, dataset_slug, no_confirm=False):
        self.dataset_delete_calls.append(
            (owner_slug, dataset_slug, no_confirm))
        if self._dataset_delete_error is not None:
            raise self._dataset_delete_error
        return self._dataset_delete_result


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


# ---------------------------------------------- dataset_file_size (Task 5) --
# dataset_reachable() only proves an account can see A copy of the dataset,
# not that it's the RIGHT one. dataset_file_size() is the size signal fleet
# uses to catch a stale copy before any kernel is pushed. Built on the same
# dataset_list_files() call -- no new API surface.

def test_dataset_file_size_returns_bytes_for_a_known_file():
    resp = FakeListFilesResponse([FakeDatasetFile("scene.blend", 1048576)])
    c, _ = client(list_files_response=resp)
    assert c.dataset_file_size("owner/x", "scene.blend") == 1048576


def test_dataset_file_size_returns_none_for_an_unlisted_file():
    """The dataset is reachable and has files -- just not one with this
    name (typo, rename, or a grant that hasn't reached the file yet)."""
    resp = FakeListFilesResponse([FakeDatasetFile("other.blend", 500)])
    c, _ = client(list_files_response=resp)
    assert c.dataset_file_size("owner/x", "scene.blend") is None


def test_dataset_file_size_returns_none_when_dataset_has_no_files_at_all():
    c, _ = client()  # default FakeApi: dataset_list_files -> {"datasetFiles": []}
    assert c.dataset_file_size("owner/x", "scene.blend") is None


def test_dataset_file_size_propagates_when_the_account_cannot_reach_the_dataset():
    """A total-unreachable failure (403) is a DIFFERENT problem than 'missing
    from an otherwise-readable listing' -- it must not be swallowed into a
    quiet None here. Callers that need to tell these apart call
    dataset_reachable() first (fleet.launch does)."""
    c, _ = client(list_files_ok=False)
    with pytest.raises(RuntimeError, match="Forbidden"):
        c.dataset_file_size("owner/x", "scene.blend")


def test_installed_kagglesdk_dataset_file_exposes_no_content_hash():
    """Documents the fact that drove dataset_file_size's design: checked
    against the actually-installed kagglesdk, ApiDatasetFile carries no
    hash/checksum/etag field, only total_bytes -- so a content-hash check
    is not available and dataset_file_size must not claim to be one. If a
    future kagglesdk release adds one, this test fails and
    dataset_file_size should be upgraded to use it."""
    from kagglesdk.datasets.types.dataset_api_service import ApiDatasetFile

    f = ApiDatasetFile()
    fields = {name for name in dir(f) if not name.startswith("_")}
    hash_like = {n for n in fields
                if any(k in n.lower() for k in ("hash", "checksum", "etag", "md5", "sha1", "sha256"))}
    assert hash_like == set(), (
        f"kagglesdk now exposes {hash_like} -- upgrade dataset_file_size "
        "to use it instead of a size-only comparison")


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


# ------------------------------------------------- list/delete (Task 8) --
# The scene library needs to show and manage an account's own datasets.
# list_datasets() wraps dataset_list(user=...) (NOT mine=True -- see its
# docstring); delete_dataset() wraps dataset_delete(..., no_confirm=True)
# and must turn a bare Kaggle permission refusal into words, since deletion
# only ever works with the dataset's OWNER token.

def test_listing_datasets_reads_the_fields_the_ui_needs():
    """The installed SDK takes page/max_size and NOT page_size -- passing
    page_size raises TypeError, which is how the first probe of this
    failed (2026-08-12)."""
    last_updated = _dt.datetime(2026, 8, 1, 12, 0, 0)
    ds = FakeDataset(ref="stivestivewithani/my-scene-blend",
                     title="my scene", total_bytes=123456,
                     last_updated=last_updated, is_private=True,
                     owner_ref="stivestivewithani")
    c, api = client(datasets=[ds])

    got = c.list_datasets()

    assert got == [DatasetInfo(ref="stivestivewithani/my-scene-blend",
                               title="my scene", total_bytes=123456,
                               last_updated=last_updated, is_private=True,
                               owner="stivestivewithani")]
    # user=<this account's own handle>, not mine=True -- see list_datasets'
    # own docstring for why whoami()'s answer is the one trusted here.
    assert api.dataset_list_calls == [
        {"user": "stivestivewithani", "mine": False}]


def test_deleting_a_dataset_never_prompts():
    """no_confirm=True stops the CLI prompting at a terminal nobody is
    watching. The confirmation is the app's own, in the UI, where the
    consequence can be spelled out."""
    c, api = client()

    c.delete_dataset("stivestivewithani/old-scene-blend")

    assert api.dataset_delete_calls == [
        ("stivestivewithani", "old-scene-blend", True)]


def test_deleting_reports_a_refusal_in_words():
    """A friend's token cannot delete another account's dataset -- Kaggle
    refuses with a bare 403 that, left unwrapped, reads exactly like a bug
    in BlendFleet rather than what it actually is: the wrong account's
    token was used for an owner-only call."""
    error = RuntimeError(
        "403 Client Error: Forbidden for url: "
        "https://api.kaggle.com/v1/datasets.DatasetApiService/"
        "DeleteDataset")
    c, api = client(dataset_delete_error=error)

    with pytest.raises(KaggleError) as excinfo:
        c.delete_dataset("someone-else/their-scene-blend")

    message = str(excinfo.value)
    assert "permission" in message.lower()
    assert "OWNER" in message
    assert "Nothing was deleted" in message


def test_deleting_reports_other_failures_without_claiming_permission(tmp_path):
    """A failure that is NOT a 403 must not be mislabeled as a permission
    problem -- that would send the user to switch accounts for a totally
    unrelated error (e.g. a transient network blip)."""
    error = RuntimeError("connection reset")
    c, api = client(dataset_delete_error=error)

    with pytest.raises(KaggleError) as excinfo:
        c.delete_dataset("stivestivewithani/old-scene-blend")

    message = str(excinfo.value)
    assert "permission" not in message.lower()
    assert "connection reset" in message
    assert "Nothing was" in message


def test_deleting_raises_when_kaggle_reports_it_as_cancelled_not_deleted():
    """Fix round 1: dataset_delete's own docstring says 'Returns True if
    deleted, False if cancelled' -- a falsy return used to be discarded,
    so delete_dataset returned cleanly (success) even though Kaggle says
    nothing happened. Not reachable through the installed SDK today (this
    call passes no_confirm=True, leaving nothing to cancel) but this is
    the single most consequential path in the module: it must never read
    a falsy result as success, whatever future SDK route produces one."""
    c, api = client(dataset_delete_result=False)

    with pytest.raises(KaggleError) as excinfo:
        c.delete_dataset("stivestivewithani/old-scene-blend")

    message = str(excinfo.value)
    assert "cancelled" in message.lower()
    assert "Nothing was deleted" in message
    # the call still happened -- this is about the RESULT, not a refusal
    # to even try
    assert api.dataset_delete_calls == [
        ("stivestivewithani", "old-scene-blend", True)]


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


def test_verify_token_reports_a_revoked_token_as_revoked():
    """A 401 must not be swallowed -- and is now NAMED.

    This test's fake was always called RevokedApi: a 401 from
    kernels_list is exactly a dead token, and the original assertion was
    only that it propagates rather than being reported as
    "accepted-but-unresolved". It still propagates; it now arrives as
    RevokedTokenError so the app can mark the account instead of showing
    one more indistinguishable failure. The underlying error is kept as
    the cause, so nothing about what Kaggle said is lost.
    """
    class RevokedApi(FakeApi):
        def kernels_list(self, mine=False, page_size=1):
            raise RuntimeError("401 Client Error: Unauthorized")

    revoked, _ = client(api=RevokedApi())
    with pytest.raises(RevokedTokenError) as exc_info:
        verify_token(TOKEN, client_factory=lambda t: revoked)
    assert "Unauthorized" in str(exc_info.value.__cause__)
    # Still a KaggleError, so every existing handler keeps working.
    assert isinstance(exc_info.value, KaggleError)


def test_verify_token_propagates_errors_that_are_not_about_the_token():
    """An unrelated failure keeps its own type and message.

    The counterpart to the test above: whoami() must not relabel
    everything it cannot list as a revoked token.
    """
    class BrokenApi(FakeApi):
        def kernels_list(self, mine=False, page_size=1):
            raise RuntimeError("kaggle exploded")

    broken, _ = client(api=BrokenApi())
    with pytest.raises(RuntimeError, match="kaggle exploded"):
        verify_token(TOKEN, client_factory=lambda t: broken)


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


# ---------------------------------------------------------------------------
# Task 4: kernels_output() writes the full kernel log to
# "<kernel-name>.log" as a side effect (kaggle_api_extended.py's own
# kernels_output: `log = response.log; ... out.write(log)`) -- fetch_log_tail
# reuses that exact call rather than any new API surface.
# ---------------------------------------------------------------------------

class LoggingApi(FakeApi):
    """Like FakeApi, but kernels_output also writes the kernel's log file
    -- exactly what the real kaggle package does as a side effect of that
    one call."""

    def __init__(self, *a, log_text=None, **kw):
        super().__init__(*a, **kw)
        self._log_text = log_text

    def kernels_output(self, slug, path, file_pattern=None):
        super().kernels_output(slug, path, file_pattern=file_pattern)
        if self._log_text is not None:
            # The real kaggle package writes the log UNCONDITIONALLY,
            # outside the per-file filtering loop entirely (see
            # kaggle_api_extended.py lines 6707-6739) -- never gated on
            # file_pattern, which is exactly what fetch_log_tail relies on.
            name = slug.split("/", 1)[1]
            (Path(path) / f"{name}.log").write_text(self._log_text,
                                                     encoding="utf-8")


def test_fetch_log_tail_returns_the_log_files_contents(tmp_path):
    c, _ = client(api=LoggingApi(log_text="line1\nline2\nline3"))
    text = c.fetch_log_tail("user/remember-render", tmp_path / "out")
    assert text == "line1\nline2\nline3"


def test_fetch_log_tail_returns_only_the_last_max_lines(tmp_path):
    """The log for a long render can be huge -- only the tail (the part
    that actually shows the crash/OOM/traceback) is ever needed."""
    lines = [f"line{i}" for i in range(500)]
    c, _ = client(api=LoggingApi(log_text="\n".join(lines)))
    text = c.fetch_log_tail("user/remember-render", tmp_path / "out",
                            max_lines=50)
    assert text.splitlines() == lines[-50:]


def test_fetch_log_tail_returns_empty_string_when_no_log_was_captured(tmp_path):
    """No log file at all (never started, output already pruned) is
    genuinely different from an empty log -- callers must not confuse this
    with "the log was fetched and found no cause"."""
    c, _ = client(api=FakeApi())  # writes no .log file
    text = c.fetch_log_tail("user/remember-render", tmp_path / "out")
    assert text == ""


def test_fetch_log_tail_downloads_no_output_files(tmp_path):
    """fetch_log_tail must fetch ONLY the log, never every rendered frame
    (and the Task 5 zip archive) a second time purely to show a one-line
    failure reason -- IMPORTANT 1 of the final review.

    Asserts on what the stub was actually ASKED for (the file_pattern
    kernels_output() received) and on what it actually WROTE to disk, not
    merely that a log string came back: kernels_output's log write is
    unconditional regardless of file_pattern (confirmed against the
    installed kaggle package), so a test that only checked the returned
    text would pass even if fetch_log_tail silently downloaded every
    output file too.
    """
    api = LoggingApi(log_text="boom: out of memory")
    c, _ = client(api=api)

    text = c.fetch_log_tail("user/remember-render", tmp_path / "out")

    assert text == "boom: out of memory"
    # The stub was asked for a pattern that cannot match any real filename.
    assert len(api.kernels_output_calls) == 1
    file_pattern = api.kernels_output_calls[0]
    assert file_pattern is not None
    assert re.search(file_pattern, "f_0001.png") is None
    assert re.search(file_pattern, "render-0.zip") is None
    # And, because the fake honours file_pattern exactly like the real
    # kaggle package does, no output file was actually written -- only the
    # log (which the fake writes unconditionally, exactly like the real one).
    assert api.kernels_output_written == []


def test_fetch_log_tail_cleans_up_its_scratch_directory(tmp_path):
    """`dest` (cache_dir()/work/log_<label> in production) is scratch
    space for this one call only. Before this fix, kernels_output's real
    download -- loose frames plus the Task 5 archive -- was left there
    forever, unbounded across every failed render; now nothing is left
    behind at all, success or failure."""
    api = LoggingApi(log_text="boom")
    c, _ = client(api=api)
    dest = tmp_path / "out"

    c.fetch_log_tail("user/remember-render", dest)

    assert not dest.exists()


def test_fetch_log_tail_cleans_up_even_when_there_is_no_log(tmp_path):
    c, _ = client(api=FakeApi())  # writes no .log file
    dest = tmp_path / "out"

    c.fetch_log_tail("user/remember-render", dest)

    assert not dest.exists()


# ---------------------------------------------------------------------------
# Task 5: fetch_output must also pick up the per-worker archive, alongside
# the loose images it already returns.
# ---------------------------------------------------------------------------

def test_fetch_output_returns_the_archive_too(tmp_path):
    class ZipApi(FakeApi):
        def kernels_output(self, slug, path):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "f_0001.png").write_bytes(b"PNG")
            (Path(path) / "render-0.zip").write_bytes(b"PK\x03\x04fake zip")

    c, _ = client(api=ZipApi())
    got = c.fetch_output("x/y", tmp_path / "out")
    assert sorted(p.name for p in got) == ["f_0001.png", "render-0.zip"]


# ---------------------------------------------------------------------------
# Task 6: fetch_output_with_progress -- real download progress via an
# instrumented reader (blendfleet.downloader), not kaggle's own
# kernels_output() (which pulls the whole body into memory with
# `.content`, nowhere for a progress callback to hook in).
# ---------------------------------------------------------------------------

class FakeOutputFile:
    def __init__(self, url, file_name):
        self.url = url
        self.file_name = file_name


class FakeListOutputResponse:
    def __init__(self, files, next_page_token=""):
        self.files = files
        self.next_page_token = next_page_token


class FakeGetResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def iter_content(self, chunk_size=None):
        yield self.body


class FakeGetTransport:
    def __init__(self, bodies: dict[str, bytes]):
        self._bodies = bodies
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        return FakeGetResponse(self._bodies[url])


def test_fetch_output_with_progress_downloads_via_the_listed_urls(tmp_path):
    class FakeSdk:
        class kernels:
            class kernels_api_client:
                @staticmethod
                def list_kernel_session_output(request):
                    assert request.user_name == "x"
                    assert request.kernel_slug == "y"
                    return FakeListOutputResponse(
                        [FakeOutputFile("https://x/f.zip", "f.zip")])

    c = KaggleClient(TOKEN, api_factory=lambda t: FakeApi(),
                     sdk_factory=lambda t: FakeSdk())
    transport = FakeGetTransport({"https://x/f.zip": b"PK\x03\x04zipbytes"})

    got = c.fetch_output_with_progress("x/y", tmp_path / "out",
                                       transport=transport)

    assert [p.name for p in got] == ["f.zip"]
    assert (tmp_path / "out" / "f.zip").read_bytes() == b"PK\x03\x04zipbytes"
    assert transport.urls == ["https://x/f.zip"]


def test_fetch_output_with_progress_reports_bytes_downloaded(tmp_path):
    class FakeSdk:
        class kernels:
            class kernels_api_client:
                @staticmethod
                def list_kernel_session_output(request):
                    return FakeListOutputResponse(
                        [FakeOutputFile("https://x/f.zip", "f.zip")])

    c = KaggleClient(TOKEN, api_factory=lambda t: FakeApi(),
                     sdk_factory=lambda t: FakeSdk())
    body = b"0" * (2 * 1024 * 1024)
    transport = FakeGetTransport({"https://x/f.zip": body})
    events = []

    c.fetch_output_with_progress("x/y", tmp_path / "out",
                                 on_progress=events.append, transport=transport)

    assert events
    assert events[-1].downloaded == len(body)
    assert events[-1].total == len(body)


def test_fetch_output_with_progress_follows_pagination(tmp_path):
    """The fallback (no archive -> many loose frames) can exceed one
    page's worth of output files -- list_kernel_session_output paginates
    exactly like kaggle's own kernels_output() already loops over
    next_page_token for, and this must too, or files beyond the first
    page would be silently dropped."""
    pages = [
        FakeListOutputResponse([FakeOutputFile("https://x/f1.png", "f1.png")]),
        FakeListOutputResponse([FakeOutputFile("https://x/f2.png", "f2.png")]),
    ]
    pages[0].next_page_token = "page2"
    pages[1].next_page_token = ""
    calls = []

    class FakeSdk:
        class kernels:
            class kernels_api_client:
                @staticmethod
                def list_kernel_session_output(request):
                    calls.append(request.page_token)
                    return pages[0] if not request.page_token else pages[1]

    c = KaggleClient(TOKEN, api_factory=lambda t: FakeApi(),
                     sdk_factory=lambda t: FakeSdk())
    transport = FakeGetTransport({"https://x/f1.png": b"AAA",
                                  "https://x/f2.png": b"BBB"})

    got = c.fetch_output_with_progress("x/y", tmp_path / "out",
                                       transport=transport)

    assert sorted(p.name for p in got) == ["f1.png", "f2.png"]
    assert calls == ["", "page2"]


def test_fetch_output_with_progress_filters_to_images_and_archives_only(tmp_path):
    class FakeSdk:
        class kernels:
            class kernels_api_client:
                @staticmethod
                def list_kernel_session_output(request):
                    return FakeListOutputResponse([
                        FakeOutputFile("https://x/f.zip", "f.zip"),
                        FakeOutputFile("https://x/notes.txt", "notes.txt"),
                    ])

    c = KaggleClient(TOKEN, api_factory=lambda t: FakeApi(),
                     sdk_factory=lambda t: FakeSdk())
    transport = FakeGetTransport({"https://x/f.zip": b"zip",
                                  "https://x/notes.txt": b"ignore me"})

    got = c.fetch_output_with_progress("x/y", tmp_path / "out",
                                       transport=transport)
    assert [p.name for p in got] == ["f.zip"]


def test_fetch_output_with_progress_rejects_path_escaping_file_names(tmp_path):
    """`file_name` comes straight off Kaggle's own API response -- no more
    trustworthy than a zip entry name, which collector._safe_zip_members
    already refuses to extract outside its target directory. Same defence
    here, for the same reason (Minor from the final review)."""
    class FakeSdk:
        class kernels:
            class kernels_api_client:
                @staticmethod
                def list_kernel_session_output(request):
                    return FakeListOutputResponse([
                        FakeOutputFile("https://x/good.png", "good.png"),
                        FakeOutputFile("https://x/evil.png",
                                      "../../evil.png"),
                    ])

    c = KaggleClient(TOKEN, api_factory=lambda t: FakeApi(),
                     sdk_factory=lambda t: FakeSdk())
    transport = FakeGetTransport({"https://x/good.png": b"GOOD",
                                  "https://x/evil.png": b"EVIL"})

    out = tmp_path / "out"
    got = c.fetch_output_with_progress("x/y", out, transport=transport)

    assert [p.name for p in got] == ["good.png"]
    assert transport.urls == ["https://x/good.png"]
    assert list(tmp_path.rglob("evil.png")) == [], (
        "the escaping file_name must never be written anywhere on disk")


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


# ---------------------------------------------------------------------------
# whoami's second source. Kaggle exposes no "who am I" endpoint, so the
# handle is read off the owner prefix of something the account owns. An
# account that has only ever uploaded a DATASET used to be rejected for
# having no notebooks -- and that is exactly the account this app is most
# likely to be handed, since a friend lending quota may never have written
# a notebook.
# ---------------------------------------------------------------------------

class _Ref:
    def __init__(self, ref):
        self.ref = ref


class _AuthenticatingApi(GoodApi):
    """A session that authenticates cleanly -- so whoami's answer is about
    what the account OWNS, not about whether the token works."""

    def __init__(self):
        super().__init__(TOKEN)


class NoKernelsButOneDatasetApi(_AuthenticatingApi):
    def kernels_list(self, **kwargs):
        return []

    def dataset_list(self, **kwargs):
        return [_Ref("friendly/some-dataset")]


class OwnsNothingApi(_AuthenticatingApi):
    def kernels_list(self, **kwargs):
        return []

    def dataset_list(self, **kwargs):
        return []


class NoKernelsAndNoDatasetCallApi(_AuthenticatingApi):
    def kernels_list(self, **kwargs):
        return []
    # dataset_list deliberately absent: older/newer SDKs differ.


class RealSignatureDatasetListApi(_AuthenticatingApi):
    """dataset_list with the SDK's REAL installed signature -- page/
    max_size, no page_size, and no **kwargs catch-all to hide a wrong
    argument name. Pins the regression fixed in Fix round 1: whoami()'s
    dataset fallback used to call dataset_list(mine=True, page_size=1),
    which the real API has no such parameter for and rejects with
    TypeError -- silently swallowed by the surrounding
    `except (AttributeError, TypeError): datasets = []`, so this fallback
    returned nothing in production, every time, for exactly the account it
    exists to serve (one with datasets but no notebooks yet -- see the
    commit "An account with no notebooks can now render")."""

    def kernels_list(self, **kwargs):
        return []

    def dataset_list(self, sort_by=None, size=None, file_type=None,
                     license_name=None, tag_ids=None, search=None,
                     user=None, mine=False, page=1, max_size=None,
                     min_size=None):
        return [_Ref("friendly/some-dataset")]


def test_whoami_falls_back_to_datasets_when_there_are_no_notebooks(monkeypatch):
    _install_fake_kaggle_package(monkeypatch, NoKernelsButOneDatasetApi())
    assert KaggleClient(TOKEN).whoami() == "friendly"


def test_whoami_dataset_fallback_works_against_the_real_sdk_signature(monkeypatch):
    """Fix round 1: before the fix, this raised KaggleError('no notebooks
    or datasets') instead of resolving 'friendly' -- the old
    page_size=1 argument doesn't exist on the real dataset_list(), so it
    always TypeError'd and the fallback silently produced []."""
    _install_fake_kaggle_package(monkeypatch, RealSignatureDatasetListApi())
    assert KaggleClient(TOKEN).whoami() == "friendly"


def test_whoami_survives_an_sdk_without_dataset_list(monkeypatch):
    """The guard around the second source is for API-surface differences
    only -- it must not turn a missing method into a crash."""
    _install_fake_kaggle_package(monkeypatch, NoKernelsAndNoDatasetCallApi())
    with pytest.raises(KaggleError, match="no notebooks or datasets"):
        KaggleClient(TOKEN).whoami()


def test_whoami_explains_what_to_do_when_the_account_owns_nothing(monkeypatch):
    """This is a perfectly ordinary account, not a broken one, so the
    message has to name both ways out rather than just refusing."""
    _install_fake_kaggle_package(monkeypatch, OwnsNothingApi())
    with pytest.raises(KaggleError) as excinfo:
        KaggleClient(TOKEN).whoami()
    message = str(excinfo.value)
    assert "username by hand" in message
    assert "Instances" in message


def test_a_bad_token_still_reports_ITS_error_not_the_owns_nothing_one(monkeypatch):
    """The regression this nearly shipped: guarding both sources swallowed
    a genuine auth failure and reported it as "this account owns nothing",
    which sends the user to fix entirely the wrong thing."""
    _install_fake_kaggle_package(monkeypatch, LegacyApiKeyFallthroughApi())
    with pytest.raises(KaggleError) as excinfo:
        KaggleClient(TOKEN, label="james").whoami()
    assert "owns nothing" not in str(excinfo.value)
    assert "no notebooks or datasets" not in str(excinfo.value)
