import ast, json
from pathlib import Path
import pytest
from blendfleet.notebook_builder import RenderSettings, build


@pytest.fixture
def settings():
    return RenderSettings(resolution_x=1920, resolution_y=1080, samples=128)


def cells_src(path):
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def strip_comments(src):
    # Naive '#'-to-end-of-line stripper. Safe here: none of the generated
    # code embeds a literal '#' inside a string, only in prose comments.
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def test_every_cell_is_valid_python(tmp_path, settings):
    p = build([1, 4, 7], settings, "me/remember-blend", tmp_path, "me/render-0")
    for src in cells_src(p):
        ast.parse(src)


def test_frame_list_is_embedded_verbatim(tmp_path, settings):
    p = build([1, 4, 7], settings, "me/x", tmp_path, "me/render-0")
    assert "FRAMES = [1, 4, 7]" in "\n".join(cells_src(p))


def test_walks_input_instead_of_hardcoding_path(tmp_path, settings):
    # /kaggle/input/datasets/<owner>/<slug>/ -- a hardcoded path broke a real run
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "os.walk" in joined
    assert '"/kaggle/input/' + 'x"' not in joined
    # the realistic hardcoded-path bug bakes in the "datasets/<owner>/<slug>" mount
    # prefix -- the code must never spell that out, only discover it via os.walk.
    # (checked against code only; the module's own explanatory comment about
    # the mount layout legitimately mentions this path in prose.)
    assert "/kaggle/input/datasets" not in strip_comments(joined)


def test_enables_only_chosen_backend_devices(tmp_path, settings):
    # d.type != "CPU" switched the same P100 on twice
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'd.use = (d.type == chosen)' in joined
    assert 'd.use = (d.type != "CPU")' not in joined


def test_copies_out_of_readonly_input(tmp_path, settings):
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "shutil.copy" in joined and "/kaggle/tmp" in joined


def test_kernel_metadata(tmp_path, settings):
    build([1], settings, "me/remember-blend", tmp_path, "me/render-0")
    meta = json.loads((tmp_path / "kernel-metadata.json").read_text())
    assert meta["enable_gpu"] is True
    assert meta["enable_internet"] is True
    assert meta["is_private"] is True
    assert meta["id"] == "me/render-0"
    assert meta["dataset_sources"] == ["me/remember-blend"]


def test_settings_with_double_quote_still_parse(tmp_path):
    # file_format / blender_version are interpolated into a generated string
    # literal. Task 9 wires these to UI fields, so a stray double quote must
    # not break the generated cell's syntax -- values must be repr()'d, not
    # dropped straight into a "..." literal.
    settings = RenderSettings(resolution_x=1920, resolution_y=1080, samples=128,
                              file_format='PN"G', blender_version='5.2".0')
    p = build([1], settings, "me/x", tmp_path, "me/r")
    for src in cells_src(p):
        ast.parse(src)


def test_progress_print_flushes(tmp_path, settings):
    # Without flush=True, Python buffers stdout and the desktop app's live
    # log stream sees nothing until the kernel exits -- the render appears
    # frozen for its entire duration.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    # one flush=True each for PREFLIGHT, PREFLIGHT_FAIL (see
    # test_preflight_* below), PROGRESS, and TELEMETRY (see test_telemetry_*
    # below) -- never more, never fewer.
    assert joined.count("flush=True") == 4, "expected exactly four flush=True"
    idx_progress = joined.index('print(f"PROGRESS')
    idx_flush = joined.index("flush=True", idx_progress)
    # flush=True must belong to the PROGRESS print call itself, not some
    # unrelated statement elsewhere in the notebook.
    assert idx_progress < idx_flush < idx_progress + 200


def test_telemetry_print_flushes(tmp_path, settings):
    # Same non-negotiable as PROGRESS: without flush=True nothing streams
    # live and the GPU panel would look frozen.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    idx_telemetry = joined.index('print(f"TELEMETRY')
    idx_flush = joined.index("flush=True", idx_telemetry)
    assert idx_telemetry < idx_flush < idx_telemetry + 300


def test_telemetry_is_per_gpu_not_aggregated(tmp_path, settings):
    # Kaggle's GPU allocation isn't guaranteed (a GPU request once returned
    # a single P100 instead of the expected T4 x2) -- each physical GPU must
    # report its own line, keyed by index, never a fleet-wide average.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "query-gpu=index,utilization.gpu,memory.used,memory.total" in joined
    assert 'f"TELEMETRY gpu={idx}' in joined


def test_telemetry_thread_is_background_and_daemon(tmp_path, settings):
    # Must not interfere with rendering: runs on its own thread, and must
    # not keep the kernel alive if the main render logic exits first.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "threading.Thread(target=_telemetry_loop" in joined
    assert "daemon=True" in joined
    assert "_telemetry_stop.set()" in joined


def test_telemetry_degrades_silently_without_nvidia_smi(tmp_path, settings):
    # This dev machine genuinely has no nvidia-smi on PATH (verified with
    # `which nvidia-smi` -> not found), so executing the generated telemetry
    # cell here exercises the real CPU-only-session path end to end: no
    # mocking of subprocess, no crash, thread exits quietly on its own.
    cells = cells_src(build([1], settings, "me/x", tmp_path, "me/r"))
    telemetry_src = next(c for c in cells if "_telemetry_loop" in c)
    ns: dict = {}
    exec(compile(telemetry_src, "<telemetry_cell>", "exec"), ns)
    ns["_telemetry_thread"].join(timeout=10)
    assert not ns["_telemetry_thread"].is_alive(), (
        "telemetry thread should exit quietly when nvidia-smi is absent")


def test_no_hardcoded_gpu_model_in_device_selection(tmp_path, settings):
    # GPU model is not guaranteed (a request for GPU returned a single P100,
    # not the T4 x2 that was expected) -- device selection must never assume
    # a specific card. (checked against code only; a prose comment
    # legitimately references the P100 incident that motivated this rule.)
    code = strip_comments("\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r"))))
    for model in ("P100", "T4", "V100", "A100"):
        assert model not in code


def test_setup_script_reports_every_enabled_device_by_name(tmp_path, settings):
    # Task 1 step 3: a single-GPU session must be visible IN THE LOG, not
    # inferred from a slow render. docs/machine-shape-findings.md confirmed
    # this line already does that correctly on a live 2-GPU run
    # ("[setup] OPTIX -> ['Tesla T4', 'Tesla T4']") -- no Cycles-side bug,
    # nothing to fix here, only to pin.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'print("[setup] " + chosen + " -> " +' in joined
    assert "str([d.name for d in prefs.devices if d.use])" in joined


# --------------------------------------------------------------------------
# Task 1: request the right machine (docs/machine-shape-findings.md)
# --------------------------------------------------------------------------

def test_kernel_metadata_pins_the_exact_valid_machine_shape(tmp_path, settings):
    # Measured live: "NvidiaTeslaT4" is the ONLY machine_shape string of the
    # three valid ones that yields 2 GPUs (2x Tesla T4). An invalid string
    # (a typo, e.g. "NvidiaTeslaT4x2") is accepted by Kaggle with NO error
    # at push time and silently falls back to a single P100 -- indistin-
    # guishable from success in the push response -- so this must be an
    # exact match, not a "looks like a GPU shape" substring check.
    build([1], settings, "me/remember-blend", tmp_path, "me/render-0")
    meta = json.loads((tmp_path / "kernel-metadata.json").read_text())
    assert meta["machine_shape"] == "NvidiaTeslaT4"


def test_kernel_metadata_keeps_enable_gpu_alongside_machine_shape(tmp_path, settings):
    # enable_gpu is deprecated but dropping it would risk a CPU session on
    # a backend that has not adopted machine_shape yet -- keep both.
    build([1], settings, "me/remember-blend", tmp_path, "me/render-0")
    meta = json.loads((tmp_path / "kernel-metadata.json").read_text())
    assert meta["enable_gpu"] is True
    assert meta["machine_shape"] == "NvidiaTeslaT4"


# --------------------------------------------------------------------------
# Task 2: preflight the session, then render or stop
# --------------------------------------------------------------------------

def test_preflight_line_precedes_the_blender_download(tmp_path, settings):
    # The whole point: hardware facts arrive before a single byte of
    # Blender is downloaded, not after.
    cells = cells_src(build([1], settings, "me/x", tmp_path, "me/r"))
    preflight_idx = next(i for i, c in enumerate(cells) if "PREFLIGHT gpus=" in c)
    download_idx = next(i for i, c in enumerate(cells) if "wget" in c)
    assert preflight_idx < download_idx


def test_preflight_reports_gpu_names_count_cpu_and_ram(tmp_path, settings):
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'print(f"PREFLIGHT gpus={len(gpu_names)} "' in joined
    assert "gpu_names={'|'.join(gpu_names) if gpu_names else 'none'}" in joined
    assert "cpu={cpu_count}" in joined
    assert "ram={ram_total:.1f}" in joined


def test_preflight_print_flushes(tmp_path, settings):
    # Same non-negotiable as PROGRESS/TELEMETRY: without flush=True this is
    # the one line meant to arrive in the first seconds, and it would not.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    idx_preflight = joined.index('print(f"PREFLIGHT')
    idx_flush = joined.index("flush=True", idx_preflight)
    assert idx_preflight < idx_flush < idx_preflight + 300


def test_min_gpus_defaults_to_zero_no_gate(tmp_path, settings):
    # settings fixture does not set min_gpus -- must default to 0 (no gate),
    # i.e. today's behaviour for every existing caller of RenderSettings.
    assert settings.min_gpus == 0
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "MIN_GPUS = 0" in joined


def test_min_gpus_is_embedded_verbatim_from_settings(tmp_path):
    settings = RenderSettings(resolution_x=1920, resolution_y=1080,
                              samples=128, min_gpus=2)
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "MIN_GPUS = 2" in joined


def test_minimum_hardware_gate_raises_before_blender_download(tmp_path):
    settings = RenderSettings(resolution_x=1920, resolution_y=1080,
                              samples=128, min_gpus=2)
    cells = cells_src(build([1], settings, "me/x", tmp_path, "me/r"))
    gate_idx = next(i for i, c in enumerate(cells) if "raise SystemExit" in c)
    download_idx = next(i for i, c in enumerate(cells) if "wget" in c)
    assert gate_idx < download_idx
    assert "MIN_GPUS" in cells[gate_idx]
    assert "len(gpu_names) < MIN_GPUS" in cells[gate_idx]


def test_preflight_fail_print_also_flushes(tmp_path):
    settings = RenderSettings(resolution_x=1920, resolution_y=1080,
                              samples=128, min_gpus=2)
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    idx_fail = joined.index('print(f"PREFLIGHT_FAIL')
    idx_flush = joined.index("flush=True", idx_fail)
    assert idx_fail < idx_flush < idx_fail + 200


def test_preflight_cell_is_still_valid_python_with_a_gate_configured(tmp_path):
    # Guards against the standing f-string/nested-quote injection risk this
    # module carries: a configured gate must not break ast.parse().
    settings = RenderSettings(resolution_x=1920, resolution_y=1080,
                              samples=128, min_gpus=2)
    p = build([1], settings, "me/x", tmp_path, "me/r")
    for src in cells_src(p):
        ast.parse(src)


# --------------------------------------------------------------------------
# Task 5: zip the rendered frames into a single per-worker archive, without
# ever losing the loose files a timeout would otherwise still leave behind.
# --------------------------------------------------------------------------

def test_writes_a_zip_archive_using_stdlib_zipfile(tmp_path, settings):
    cells = cells_src(build([1], settings, "me/x", tmp_path, "me/r"))
    render_cell = next(c for c in cells if "PROGRESS frame=" in c)
    assert "zipfile" in render_cell.splitlines()[0], \
        "zipfile must be imported (stdlib -- no dependency on either side)"
    assert "zipfile.ZipFile" in render_cell


def test_archive_uses_zip_stored_not_deflate(tmp_path, settings):
    # PNG/JPEG are already compressed -- deflating them costs render-loop
    # time for almost nothing. The whole win here is one download instead
    # of hundreds, not squeezing more bytes out of an already-compressed
    # format.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "zipfile.ZIP_STORED" in joined
    assert "ZIP_DEFLATED" not in joined


def test_archive_name_is_per_worker_not_a_fixed_shared_name(tmp_path, settings):
    # Named from kernel_slug (unique per worker/account) rather than a
    # constant like "frames.zip" -- collect() must be able to tell one
    # worker's archive from another's if they ever land in the same place.
    joined = "\n".join(cells_src(
        build([1], settings, "me/x", tmp_path, "me/render-abc123")))
    assert "render-abc123" in joined


def test_archive_is_written_inside_the_frame_loop_not_only_at_the_end(tmp_path, settings):
    # THE non-negotiable from the brief: if the archive were written only
    # once after the whole FRAMES loop finishes, a session that hits the
    # wall or dies mid-render would lose every frame's inclusion in the
    # archive -- the archive write must happen as each frame completes, so
    # a partial render still produces a partial (not empty, not missing)
    # archive alongside the loose files.
    cells = cells_src(build([1, 2, 3], settings, "me/x", tmp_path, "me/r"))
    render_cell = next(c for c in cells if "PROGRESS frame=" in c)
    zip_idx = render_cell.index("zipfile.ZipFile")
    loop_idx = render_cell.index("for frame in FRAMES")
    done_idx = render_cell.index('print("DONE"')
    assert loop_idx < zip_idx < done_idx, (
        "the archive write must sit inside the per-frame loop, strictly "
        "before the final DONE summary")


def test_archive_write_does_not_replace_the_loose_frames(tmp_path, settings):
    # The loose files in OUT must still be written exactly as before --
    # the archive is written IN ADDITION, never as a replacement, so a
    # timeout that kills the session before the archive is even opened
    # still leaves every finished frame recoverable.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'os.makedirs(OUT, exist_ok=True)' in joined
    assert "BR_OUTPUT" in joined
    assert "shutil.rmtree" not in joined
    assert "os.remove(f\"{OUT}" not in joined


def test_archive_only_includes_successfully_rendered_frames(tmp_path, settings):
    # A failed frame's (nonexistent) output must never be swept into the
    # archive -- only zip on ok, exactly like `done`/`failed` already track.
    cells = cells_src(build([1], settings, "me/x", tmp_path, "me/r"))
    render_cell = next(c for c in cells if "PROGRESS frame=" in c)
    ok_idx = render_cell.index("if ok:")
    zip_idx = render_cell.index("zipfile.ZipFile")
    assert ok_idx < zip_idx


def test_every_cell_is_still_valid_python_with_archiving(tmp_path, settings):
    p = build([1, 2, 3], settings, "me/remember-blend", tmp_path, "me/render-0")
    for src in cells_src(p):
        ast.parse(src)
