import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from blendfleet.ui.upload_view import STATE_COMPLETE, STATE_FAILED, \
    STATE_IDLE, STATE_UPLOADING, UploadView
from blendfleet.uploader import UploadProgress

MB = 1 << 20


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_empty_state_shows_placeholder(qapp):
    view = UploadView()
    assert not view._placeholder.isHidden()
    assert view._rows == {}


def test_row_created_on_demand_starts_idle(qapp):
    view = UploadView()
    row = view.ensure_row("you")
    assert row.state == STATE_IDLE
    assert view._placeholder.isHidden()


def test_in_progress_updates_bar_and_stats(qapp):
    view = UploadView()
    view.update_progress("you", UploadProgress(
        uploaded=int(41.2 * MB), total=int(63.1 * MB),
        rate_bps=2.1 * MB, retries=0, resumed_from=0))
    row = view.ensure_row("you")
    assert row.state == STATE_UPLOADING
    assert row.bar.value() == pytest.approx(int(41.2 / 63.1 * 1000), abs=2)
    assert "41.2 MB" in row.stats_label.text()
    assert "63.1 MB" in row.stats_label.text()
    assert "2.1 MB/s" in row.stats_label.text()


def test_retrying_is_visibly_different_from_slow():
    # "slow" (a low but nonzero rate) vs "stuck" (rate stalled, retries
    # climbing) must produce different text -- that is the entire point
    # of this view, per the brief.
    app = QApplication.instance() or QApplication([])
    view = UploadView()
    view.update_progress("you", UploadProgress(
        uploaded=10 * MB, total=63 * MB, rate_bps=0, retries=3,
        resumed_from=5 * MB))
    text = view.ensure_row("you").stats_label.text()
    assert "stalled" in text
    assert "retries 3" in text
    assert "resumed from 5.0 MB" in text


def test_complete_state():
    app = QApplication.instance() or QApplication([])
    view = UploadView()
    view.update_progress("you", UploadProgress(
        uploaded=10 * MB, total=10 * MB, rate_bps=1 * MB, retries=0,
        resumed_from=0))
    view.set_complete("you")
    row = view.ensure_row("you")
    assert row.state == STATE_COMPLETE
    assert row.bar.value() == 1000
    assert "done" in row.stats_label.text().lower()


def test_failed_state_shows_friendly_message_not_raw_exception():
    app = QApplication.instance() or QApplication([])
    view = UploadView()
    friendly = ("the .blend file did not finish uploading to Kaggle, so "
               "the request was submitted with no file attached. Retry "
               "the render.")
    view.set_failed("you", friendly)
    row = view.ensure_row("you")
    assert row.state == STATE_FAILED
    assert row.stats_label.text() == friendly


def test_clear_resets_to_placeholder():
    app = QApplication.instance() or QApplication([])
    view = UploadView()
    view.ensure_row("you")
    view.clear()
    assert view._rows == {}
    assert not view._placeholder.isHidden()


def test_multiple_accounts_tracked_independently():
    app = QApplication.instance() or QApplication([])
    view = UploadView()
    view.update_progress("you", UploadProgress(
        uploaded=5 * MB, total=10 * MB, rate_bps=1 * MB, retries=0,
        resumed_from=0))
    view.set_failed("anna", "anna's upload could not be verified -- retry.")
    assert view.ensure_row("you").state == STATE_UPLOADING
    assert view.ensure_row("anna").state == STATE_FAILED
