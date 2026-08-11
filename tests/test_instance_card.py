import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import blendfleet.ui.theme as theme
from blendfleet.accounts import Account
from blendfleet.fleet import WorkerState
from blendfleet.instance_state import (DEFAULT_STALE_AFTER_SECONDS,
                                       GpuSnapshot, InstanceSnapshot)
from blendfleet.ui.instance_card import (NEVER_RUN_TEXT, GpuLiveRow,
                                         InstanceCard, format_age,
                                         format_hardware_summary,
                                         format_preflight_summary, is_active,
                                         is_live, status_for)
from blendfleet.ui.theme import ACCENTS, current_accent, current_theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_active_accent(qapp):
    """test_switching_accent_actually_repaints_the_status_icon below calls
    theme.apply() with every non-default accent -- see test_theme.py's
    fixture of the same name for why that process-global state must not
    leak into other test modules that run later in the same session.

    Only actually calls theme.apply() -- which restyles the WHOLE
    QApplication, every widget any earlier test left alive under the one
    shared QApplication included -- when a test genuinely changed the
    accent; see tests/test_dashboard.py's fixture of the same name for
    the measured cost of calling it unconditionally on every teardown."""
    original = theme._active_accent_name
    yield
    if theme._active_accent_name != original:
        theme._active_accent_name = original
        theme.apply(qapp, original)


def _image_has_color(image, hex_color: str) -> bool:
    """Whether any opaque pixel in `image` is exactly `hex_color` -- the
    pixel-sampling proof the task brief asks for, not a string check
    against a stylesheet that could be right while nothing on screen
    actually is."""
    from PySide6.QtGui import QColor
    target = QColor(hex_color)
    for y in range(image.height()):
        for x in range(image.width()):
            px = image.pixelColor(x, y)
            if px.alpha() > 0 and (px.red(), px.green(), px.blue()) == \
                    (target.red(), target.green(), target.blue()):
                return True
    return False


def make_account(label="stive", username="stive", verified=True) -> Account:
    return Account(label=label, token="KGAT_" + "0" * 32, username=username,
                  verified=verified)


def make_worker(label="stive", username="stive", state="running",
                frames=None, frames_done=0) -> WorkerState:
    frames = frames if frames is not None else [1, 2, 3, 4]
    return WorkerState(label=label, username=username,
                       kernel_slug=f"{username}/k", frames=frames,
                       state=state, frames_done=frames_done)


def make_snapshot(observed_at=0.0, gpus=None, cpu_count=4, ram_total=31.3):
    gpus = gpus if gpus is not None else [
        GpuSnapshot(index=0, mem_total=16280, model="Tesla P100-PCIE-16GB")]
    return InstanceSnapshot(username="stive", gpus=gpus, cpu_count=cpu_count,
                            ram_total=ram_total, observed_at=observed_at)


# ---------------- pure logic ----------------

def test_format_age_buckets():
    assert format_age(0) == "just now"
    assert format_age(59) == "just now"
    assert format_age(60) == "1m ago"
    assert format_age(3599) == "59m ago"
    assert format_age(3600) == "1h ago"
    assert format_age(7200) == "2h ago"
    assert format_age(86400) == "1d ago"
    assert format_age(3 * 86400) == "3d ago"


def test_format_age_never_negative():
    assert format_age(-5) == "just now"


def test_format_hardware_summary_full():
    snap = make_snapshot()
    text = format_hardware_summary(snap)
    assert "Tesla P100-PCIE-16GB" in text
    assert "4 vCPU" in text
    assert "31.3 GB" in text


def test_format_hardware_summary_missing_model_reads_as_gpu_not_blank():
    snap = make_snapshot(gpus=[GpuSnapshot(index=0, mem_total=16280, model=None)])
    text = format_hardware_summary(snap)
    assert "GPU" in text


def test_format_hardware_summary_nothing_known():
    snap = InstanceSnapshot(username=None, gpus=[], cpu_count=None,
                            ram_total=None, observed_at=0.0)
    assert format_hardware_summary(snap) == "hardware details unavailable"


def test_format_preflight_summary_with_two_identical_gpus():
    text = format_preflight_summary({
        "gpu_count": 2, "gpu_names": ["Tesla T4", "Tesla T4"],
        "cpu_count": 4, "ram_total": 31.3})
    assert "2x Tesla T4" in text
    assert "4 vCPU" in text
    assert "31.3 GB RAM" in text


def test_format_preflight_summary_single_gpu_not_counted():
    text = format_preflight_summary({
        "gpu_count": 1, "gpu_names": ["Tesla P100-PCIE-16GB"],
        "cpu_count": 4, "ram_total": 31.3})
    assert "Tesla P100-PCIE-16GB" in text
    assert "1x" not in text


def test_format_preflight_summary_no_gpus_reads_as_cpu_only():
    text = format_preflight_summary({
        "gpu_count": 0, "gpu_names": [], "cpu_count": 4, "ram_total": 31.3})
    assert "CPU only" in text


def test_status_for_idle_is_symbol_and_word():
    icon_name, colour, word = status_for(None)
    assert word == "idle"
    assert colour == current_theme().ink_3
    assert icon_name


def test_status_for_running_is_rendering():
    _, colour, word = status_for(make_worker(state="running"))
    assert word == "rendering"
    assert colour == current_accent().base


def test_status_for_error_is_amber_not_red():
    """Failure must be amber even though the accent could be the red
    palette -- the warn tokens are fixed independently of the active accent
    (theme.ThemePalette)."""
    _, colour, word = status_for(make_worker(state="error"))
    assert word == "error"
    assert colour == current_theme().warn_ink


def test_status_for_cancelled_states():
    for state in ("cancel_requested", "cancel_acknowledged"):
        _, _, word = status_for(make_worker(state=state))
        assert word == "cancelled"


def test_status_for_complete():
    _, _, word = status_for(make_worker(state="complete"))
    assert word == "complete"


def test_status_for_unverified_overrides_worker_state():
    _, colour, word = status_for(make_worker(state="running"), verified=False)
    assert word == "not verified"
    assert colour == current_theme().warn_ink


def test_is_live_only_for_running():
    assert is_live(None) is False
    assert is_live(make_worker(state="queued")) is False
    assert is_live(make_worker(state="running")) is True
    assert is_live(make_worker(state="error")) is False


# ---------------- InstanceCard: idle ----------------

def test_idle_card_shows_quota_and_last_known_hardware_not_a_live_gauge(qapp):
    card = InstanceCard(0, make_account())
    card.set_quota("2.4 / 30.0 h")
    card.set_snapshot(make_snapshot(observed_at=1000.0), now=1000.0 + 7200.0)
    card.set_worker(None)

    assert card.status_word.text() == "idle"
    assert "live" in card.quota_marker.text()
    assert "2h ago" in card.last_run_value.text()
    assert "Tesla P100-PCIE-16GB" in card.last_run_value.text()
    assert "4 vCPU" in card.last_run_value.text()
    assert "31.3 GB" in card.last_run_value.text()

    # the whole point: an idle card must not carry a live gauge
    assert card._gpu_rows == {}
    assert card.idle_container.isHidden() is False
    assert card.live_container.isHidden() is True


def test_never_run_account_reads_the_exact_placeholder(qapp):
    card = InstanceCard(0, make_account())
    card.set_snapshot(None)
    card.set_worker(None)
    assert card.last_run_value.text() == NEVER_RUN_TEXT


def test_stale_snapshot_is_visibly_marked(qapp):
    card = InstanceCard(0, make_account())
    card.set_snapshot(make_snapshot(observed_at=0.0),
                      now=DEFAULT_STALE_AFTER_SECONDS + 1.0)
    text = card.last_run_value.text()
    assert "stale" in text.lower()
    assert current_theme().warn_ink in card.last_run_value.styleSheet()


def test_fresh_snapshot_is_not_marked_stale(qapp):
    card = InstanceCard(0, make_account())
    card.set_snapshot(make_snapshot(observed_at=1000.0), now=1000.0 + 60.0)
    assert "stale" not in card.last_run_value.text().lower()


# ---------------- InstanceCard: rendering ----------------

def test_rendering_card_shows_live_util_and_vram(qapp):
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running", frames=[1, 2, 3, 4],
                                frames_done=1))
    card.ingest_telemetry({"gpu": 0, "util": 87, "mem_used": 6144,
                          "mem_total": 15360, "temp": 71, "power": 58.0})

    assert card.status_word.text() == "rendering"
    assert card.idle_container.isHidden() is True
    assert card.live_container.isHidden() is False
    assert 0 in card._gpu_rows
    row = card._gpu_rows[0]
    assert row.util_value.text().strip() == "87%"
    assert "6.0 GB" in row.mem_value.text()
    assert "15.0 GB" in row.mem_value.text()
    assert "live" in row.live_marker.text()
    assert card.frames_label.text() == "1 / 4"


def test_two_gpus_render_as_two_rows(qapp):
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    card.ingest_telemetry({"gpu": 0, "util": 50, "mem_used": 100,
                          "mem_total": 200, "temp": 60, "power": 10.0})
    card.ingest_telemetry({"gpu": 1, "util": 12, "mem_used": 80,
                          "mem_total": 200, "temp": 55, "power": 8.0})
    assert set(card._gpu_rows.keys()) == {0, 1}
    assert len(card._gpu_rows) == 2


def test_ingest_telemetry_is_ignored_while_idle(qapp):
    """Defense in depth: even if a caller mistakenly routes a telemetry
    record to an idle card, it must not grow a live gauge."""
    card = InstanceCard(0, make_account())
    card.set_worker(None)
    card.ingest_telemetry({"gpu": 0, "util": 99, "mem_used": 1, "mem_total": 2,
                          "temp": 1, "power": None})
    assert card._gpu_rows == {}
    assert card.live_container.isHidden() is True


def test_going_idle_after_rendering_clears_the_live_gpu_rows(qapp):
    """A completed/errored/cancelled render must not leave the PREVIOUS
    run's gauges sitting (hidden) in the live body -- the next time this
    account renders it starts from nothing, honestly."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    card.ingest_telemetry({"gpu": 0, "util": 50, "mem_used": 100,
                          "mem_total": 200, "temp": 60, "power": 10.0})
    assert card._gpu_rows

    card.set_worker(make_worker(state="complete"))
    assert card._gpu_rows == {}
    assert card.status_word.text() == "complete"


def test_telemetry_arrival_outranks_a_stale_queued_poll(qapp):
    """dashboard.py polls kernel status only every 30s, but a kernel's
    telemetry can start within a few seconds of it actually starting -- so
    the poll can still say "queued" while TELEMETRY lines are genuinely
    arriving. The status WORD is allowed to lag (it is honestly whatever
    Kaggle's last poll said), but the live BODY must not hide real data
    behind that lag."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="queued"))
    assert card.status_word.text() == "queued"
    assert card.live_container.isHidden() is True

    card.ingest_telemetry({"gpu": 0, "util": 42, "mem_used": 100,
                          "mem_total": 200, "temp": 55, "power": 5.0})
    assert card.status_word.text() == "queued"      # word still honest
    assert card.live_container.isHidden() is False  # body no longer hidden
    assert 0 in card._gpu_rows

    # The next poll catches up to "running" -- must stay live, not flicker.
    card.set_worker(make_worker(state="running"))
    assert card.live_container.isHidden() is False
    assert 0 in card._gpu_rows


def test_a_straggling_sample_after_error_cannot_resurrect_the_live_body(qapp):
    """A TELEMETRY sample already in flight when Kaggle reports "error"
    must not reopen the live body a moment later."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="queued"))
    card.ingest_telemetry({"gpu": 0, "util": 42, "mem_used": 100,
                          "mem_total": 200, "temp": 55, "power": 5.0})
    assert card.live_container.isHidden() is False

    card.set_worker(make_worker(state="error"))
    assert card.live_container.isHidden() is True
    assert card._gpu_rows == {}

    card.ingest_telemetry({"gpu": 0, "util": 1, "mem_used": 1, "mem_total": 2,
                          "temp": 1, "power": None})
    assert card.live_container.isHidden() is True
    assert card._gpu_rows == {}


# ---------------- InstanceCard: preflight ----------------

def test_preflight_arrival_makes_the_live_body_visible_while_still_queued(qapp):
    """PREFLIGHT arrives before Blender is even downloaded -- well before
    any TELEMETRY line could exist -- so it must outrank a stale "queued"
    poll exactly like telemetry does, or the card stays stuck looking like
    it is still "starting…" for the entire download+setup window."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="queued"))
    assert card.live_container.isHidden() is True

    card.set_preflight({"gpu_count": 2, "gpu_names": ["Tesla T4", "Tesla T4"],
                        "cpu_count": 4, "ram_total": 31.3})
    assert card.live_container.isHidden() is False
    assert "2x Tesla T4" in card.preflight_label.text()
    assert card.preflight_label.isHidden() is False


def test_preflight_ignored_while_idle(qapp):
    card = InstanceCard(0, make_account())
    card.set_worker(None)
    card.set_preflight({"gpu_count": 1, "gpu_names": ["Tesla T4"],
                        "cpu_count": 4, "ram_total": 31.3})
    assert card.live_container.isHidden() is True
    assert card.preflight_label.text() == ""


def test_going_idle_after_preflight_clears_it(qapp):
    """A completed/errored/cancelled render must not leave the PREVIOUS
    run's PREFLIGHT text sitting (hidden) in the live body."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="queued"))
    card.set_preflight({"gpu_count": 1, "gpu_names": ["Tesla T4"],
                        "cpu_count": 4, "ram_total": 31.3})
    assert "Tesla T4" in card.preflight_label.text()

    card.set_worker(make_worker(state="complete"))
    assert card.preflight_label.text() == ""
    assert card.preflight_label.isHidden() is True


def test_set_preflight_none_clears_it_unconditionally(qapp):
    """dashboard.py calls set_preflight(None) at the start of every new
    render, before this run's own line can possibly have arrived -- this
    must clear even a card whose worker is currently None/stopped, unlike
    the guarded real-record path."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="queued"))
    card.set_preflight({"gpu_count": 1, "gpu_names": ["Tesla T4"],
                        "cpu_count": 4, "ram_total": 31.3})
    card.set_worker(None)
    card.set_preflight(None)
    assert card.preflight_label.text() == ""


def test_telemetry_and_preflight_can_both_be_visible(qapp):
    """PREFLIGHT reports total CPU/RAM, which no live gauge ever shows
    (see the module docstring) -- it stays up alongside per-GPU telemetry
    rows once they start arriving, rather than being replaced by them."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    card.set_preflight({"gpu_count": 1, "gpu_names": ["Tesla T4"],
                        "cpu_count": 4, "ram_total": 31.3})
    card.ingest_telemetry({"gpu": 0, "util": 50, "mem_used": 100,
                          "mem_total": 15360, "temp": 60, "power": 10.0})
    assert card.preflight_label.isHidden() is False
    assert 0 in card._gpu_rows


def test_queued_worker_shows_cached_body_not_an_empty_live_one(qapp):
    """queued means a kernel exists but nothing has run yet -- no telemetry
    can possibly have arrived, so this must not show an empty "live" body."""
    card = InstanceCard(0, make_account())
    card.set_snapshot(make_snapshot(observed_at=1000.0), now=1000.0 + 60.0)
    card.set_worker(make_worker(state="queued"))
    assert card.status_word.text() == "queued"
    assert card.idle_container.isHidden() is False
    assert card.live_container.isHidden() is True


# ---------------- quota freshness ----------------

def test_quota_unavailable_carries_no_live_marker(qapp):
    card = InstanceCard(0, make_account())
    card.set_quota("unavailable")
    assert card.quota_value.text() == "unavailable"
    assert card.quota_marker.text() == ""
    assert current_theme().warn_ink in card.quota_value.styleSheet()


def test_quota_never_fetched_shows_placeholder_not_a_fabricated_number(qapp):
    card = InstanceCard(0, make_account())
    card.set_quota(None)
    assert card.quota_value.text() == "—"
    assert card.quota_marker.text() == ""


def test_quota_value_marked_live(qapp):
    card = InstanceCard(0, make_account())
    card.set_quota("6.1 / 30.0 h")
    assert card.quota_value.text() == "6.1 / 30.0 h"
    assert card.quota_marker.text() == "live"


# ---------------- verification ----------------

def test_unverified_account_shows_not_verified_even_while_worker_is_active(qapp):
    """SetupDialog is where an account actually gets verified, but a card
    must not claim "rendering" for a token Kaggle never confirmed."""
    card = InstanceCard(0, make_account(verified=False))
    card.set_worker(make_worker(state="running"))
    assert card.status_word.text() == "not verified"


def test_set_verified_updates_an_existing_card_in_place(qapp):
    card = InstanceCard(0, make_account(verified=False))
    assert card.status_word.text() == "not verified"
    card.set_verified(True)
    assert card.status_word.text() == "idle"


# ---------------- GpuLiveRow in isolation ----------------

def test_gpu_live_row_formats_percent_and_memory(qapp):
    row = GpuLiveRow(0)
    row.update_sample(util=87, mem_used=6144, mem_total=15360)
    assert row.util_value.text().strip() == "87%"
    assert "6.0 GB" in row.mem_value.text()
    assert "15.0 GB" in row.mem_value.text()


# ---------------- proof: the accent actually reaches rendered pixels -----
# THE bug the Task 6 brief calls out by name: a reviewer confirmed on Task
# 4 that rendering with the red accent selected produced ZERO red pixels
# anywhere, because status_for()/quota_marker captured the accent at THIS
# module's own import time. status_for() now calls current_accent() at
# call time, and this samples the actual rendered status-icon pixmap
# rather than trusting a colour string.

@pytest.mark.parametrize("name", ["orange", "green", "purple", "blue", "red"])
def test_switching_accent_actually_repaints_the_status_icon(qapp, name):
    theme.apply(qapp, name)
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    image = card.status_icon.pixmap().toImage()
    assert _image_has_color(image, ACCENTS[name].base), (
        f"the {name!r} accent's own colour ({ACCENTS[name].base}) does not "
        "appear anywhere in the rendered 'rendering' status icon")


# ---------------- Task 3: per-instance Cancel control ----------------

def test_cancel_button_hidden_while_idle(qapp):
    card = InstanceCard(0, make_account())
    card.set_worker(None)
    assert card.cancel_btn.isHidden() is True


def test_cancel_button_visible_while_queued(qapp):
    """Review fix (Task 3 spec defect): a queued kernel already holds one
    of the account's 2 GPU session slots and Fleet.cancel_worker() cancels
    it correctly, so the button must be offered for "queued" too, not
    only "running" -- see the class docstring and is_active()."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="queued"))
    assert card.cancel_btn.isHidden() is False


def test_cancel_button_visible_while_running(qapp):
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    assert card.cancel_btn.isHidden() is False


@pytest.mark.parametrize("state", ["queued", "running"])
def test_is_active_true_for_queued_and_running(qapp, state):
    assert is_active(make_worker(state=state)) is True


@pytest.mark.parametrize("state", ["complete", "error", "cancel_requested",
                                   "cancel_acknowledged"])
def test_is_active_false_once_stopped(qapp, state):
    assert is_active(make_worker(state=state)) is False


def test_is_active_false_for_no_worker(qapp):
    assert is_active(None) is False


@pytest.mark.parametrize("state", ["complete", "error", "cancel_requested",
                                   "cancel_acknowledged"])
def test_cancel_button_hidden_once_stopped(qapp, state):
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    assert card.cancel_btn.isHidden() is False
    card.set_worker(make_worker(state=state))
    assert card.cancel_btn.isHidden() is True


def test_clicking_cancel_emits_the_accounts_label(qapp):
    card = InstanceCard(0, make_account(label="stive"))
    card.set_worker(make_worker(state="running"))
    seen = []
    card.cancel_requested.connect(seen.append)
    card.cancel_btn.click()
    assert seen == ["stive"]


def test_set_cancel_busy_disables_and_relabels_the_button(qapp):
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    assert card.cancel_btn.isEnabled() is True
    assert card.cancel_btn.text() == "Cancel"

    card.set_cancel_busy(True)
    assert card.cancel_btn.isEnabled() is False
    assert card.cancel_btn.text() == "Cancelling…"

    card.set_cancel_busy(False)
    assert card.cancel_btn.isEnabled() is True
    assert card.cancel_btn.text() == "Cancel"


def test_going_non_running_resets_any_leftover_busy_state(qapp):
    """A poll landing mid-flight (worker goes from "running" to "error")
    must not leave the button permanently stuck disabled the next time
    this account renders and the card is reused."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    card.set_cancel_busy(True)

    card.set_worker(make_worker(state="error"))
    assert card.cancel_btn.isEnabled() is True
    assert card.cancel_btn.text() == "Cancel"


# ---------------- Task 4: one-line failure cause + full log -----------

def test_set_failure_shows_a_translated_one_line_cause_not_raw_text(qapp):
    card = InstanceCard(0, make_account())
    card.set_failure("CUDA out of memory: tried to allocate 2.00 GiB")
    assert card.failure_label.isHidden() is False
    assert "Ran out of memory" in card.failure_label.text()
    assert "CUDA out of memory" not in card.failure_label.text()
    assert card.view_log_btn.isHidden() is False


def test_set_failure_symbol_and_word_never_colour_alone(qapp):
    """The symbol is the bundled triangle-alert icon beside the text, not a
    "⚠" character in it: Roboto has no glyph for U+26A0, so the character
    version drew a tofu box -- i.e. no symbol at all, leaving amber colour
    carrying the meaning on its own, which is the one thing the rule
    forbids."""
    card = InstanceCard(0, make_account())
    card.set_failure("Segmentation fault")
    assert "Blender crashed" in card.failure_label.text()
    assert not card.failure_icon.pixmap().isNull()
    assert card.failure_row.isHidden() is False


def test_view_log_button_shows_the_full_untranslated_explanation(qapp, monkeypatch):
    shown = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **kw: shown.append(a))
    card = InstanceCard(0, make_account())
    card.set_failure("CUDA out of memory: tried to allocate 2.00 GiB")
    card.view_log_btn.click()
    assert shown
    title, message = shown[0][1], shown[0][2]
    assert "memory" in message.lower()


def test_set_failure_none_clears_it(qapp):
    card = InstanceCard(0, make_account())
    card.set_failure("Segmentation fault")
    card.set_failure(None)
    assert card.failure_label.text() == ""
    assert card.failure_row.isHidden() is True
    assert card.view_log_btn.isHidden() is True


@pytest.mark.parametrize("name", ["orange", "green", "purple", "blue", "red"])
def test_switching_accent_actually_repaints_an_already_built_card(qapp, name):
    """Not just a freshly-built card -- refresh_accent() (wired to
    theme.theme_signal.changed by dashboard.py) must repaint a card built
    BEFORE the switch, which is the live-without-restart requirement."""
    card = InstanceCard(0, make_account())
    card.set_worker(make_worker(state="running"))
    theme.apply(qapp, name)
    card.refresh_accent()
    image = card.status_icon.pixmap().toImage()
    assert _image_has_color(image, ACCENTS[name].base)


# ---------------- Task 6: per-instance Download control ----------------

def test_download_button_is_always_available(qapp):
    """Unlike Cancel (gated on "running"), Download makes sense any time
    there is a job on disk at all -- idle, queued, running, complete,
    even error (partial frames are still worth grabbing) -- so it is
    never hidden by worker state."""
    card = InstanceCard(0, make_account())
    for state in (None, "queued", "running", "complete", "error"):
        card.set_worker(make_worker(state=state) if state else None)
        assert card.download_btn.isHidden() is False


def test_clicking_download_emits_the_accounts_label(qapp):
    card = InstanceCard(0, make_account(label="stive"))
    seen = []
    card.download_requested.connect(seen.append)
    card.download_btn.click()
    assert seen == ["stive"]


def test_set_download_busy_disables_and_relabels_the_button(qapp):
    card = InstanceCard(0, make_account())
    assert card.download_btn.isEnabled() is True
    assert card.download_btn.text() == "Download"

    card.set_download_busy(True)
    assert card.download_btn.isEnabled() is False
    assert card.download_btn.text() == "Downloading…"

    card.set_download_busy(False)
    assert card.download_btn.isEnabled() is True
    assert card.download_btn.text() == "Download"


def test_set_download_progress_shows_bytes_rate_and_eta(qapp):
    from blendfleet.downloader import DownloadProgress
    card = InstanceCard(0, make_account())
    card.set_download_progress(
        DownloadProgress(downloaded=2 * 1024 * 1024, total=10 * 1024 * 1024,
                         rate_bps=1024 * 1024.0))
    text = card.download_progress_label.text()
    assert "2.0 MB" in text
    assert "10.0 MB" in text
    assert "MB/s" in text
    assert card.download_progress_label.isHidden() is False


def test_set_download_progress_stalled_reads_stalled_not_zero(qapp):
    """Reusing formatting.format_rate -- never write a second copy of the
    "stalled" rule."""
    from blendfleet.downloader import DownloadProgress
    card = InstanceCard(0, make_account())
    card.set_download_progress(DownloadProgress(downloaded=0, total=10, rate_bps=0.0))
    assert "stalled" in card.download_progress_label.text()


def test_set_download_progress_none_clears_and_hides_the_line(qapp):
    from blendfleet.downloader import DownloadProgress
    card = InstanceCard(0, make_account())
    card.set_download_progress(DownloadProgress(downloaded=1, total=2, rate_bps=1.0))
    assert card.download_progress_label.isHidden() is False

    card.set_download_progress(None)
    assert card.download_progress_label.text() == ""
    assert card.download_progress_label.isHidden() is True
