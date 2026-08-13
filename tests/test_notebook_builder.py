import ast, json
from pathlib import Path
import pytest
import blendfleet.notebook_builder as nb
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
    # file_format is interpolated into a generated string literal. Task 9
    # wires it to a UI field, so a stray double quote must not break the
    # generated cell's syntax -- values must be repr()'d, not dropped
    # straight into a "..." literal. blender_version is not exercised with
    # a quote here: Task 2's validate_version now rejects anything shaped
    # like an injection (it must be major.minor.patch, digits only) before
    # the cell is ever built, so that specific field's quote-safety is
    # covered by test_an_unusable_version_is_refused_before_a_kernel_is_built
    # instead.
    settings = RenderSettings(resolution_x=1920, resolution_y=1080, samples=128,
                              file_format='PN"G')
    p = build([1], settings, "me/x", tmp_path, "me/r")
    for src in cells_src(p):
        ast.parse(src)


def test_progress_print_flushes(tmp_path, settings):
    # Without flush=True, Python buffers stdout and the desktop app's live
    # log stream sees nothing until the kernel exits -- the render appears
    # frozen for its entire duration.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    # PREFLIGHT, PREFLIGHT_FAIL, PROGRESS and TELEMETRY must each flush --
    # they are what the live log stream shows. Asserted by NAME rather than
    # by a total count: the count was a proxy that broke the moment the
    # Blender-setup cell gained its own progress prints, which flush for
    # exactly the same reason and are not a regression.
    for marker in ('print(f"PREFLIGHT', '"PROGRESS', '"TELEMETRY'):
        index = joined.index(marker)
        assert "flush=True" in joined[index:index + 220], (
            f"{marker} does not flush -- the live log would not show it")
    assert joined.count("flush=True") >= 4
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
    assert "[setup] backend={chosen} gpus={len(enabled)} devices={enabled}" \
        in joined
    assert "enabled = [d.name for d in prefs.devices if d.use]" in joined


def test_setup_line_reaches_the_log_instead_of_being_swallowed(tmp_path,
                                                               settings):
    # The line above existed for weeks and was never once visible: the
    # notebook ran Blender with stdout=PIPE and printed it ONLY on
    # failure, so on every successful render "which backend" and "how many
    # GPUs" were captured and discarded. A live 15-frame render on
    # 2026-08-11 produced no [setup] line at all in its Kaggle log.
    # The runner must echo it as it streams.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'line.startswith("[setup]")' in joined
    assert "print(line, flush=True)" in joined


def test_optix_is_preferred_over_cuda(tmp_path, settings):
    # The T4s Kaggle allocates are Turing: they have RT cores that CUDA
    # leaves idle. OptiX must be TRIED first, with CUDA as the fallback --
    # order matters, so this pins the tuple rather than mere presence.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert 'for backend in ("OPTIX", "CUDA"):' in joined


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
    runner = next(c for c in cells if "def _archive_new" in c)
    assert "zipfile" in runner.splitlines()[0], \
        "zipfile must be imported (stdlib -- no dependency on either side)"
    assert "zipfile.ZipFile" in runner


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


def test_archive_name_actually_differs_between_workers_in_the_same_job(tmp_path, settings):
    # Final review Minor: kernel_slug is "<username>/<stem>-render-<job_id>"
    # -- username is the only part that differs between two workers in the
    # SAME job (stem and job_id are fleet-wide). Slicing the owner off with
    # kernel_slug.split("/", 1)[1] left every worker's archive_name
    # identical despite a comment here claiming otherwise; this locks in
    # that two different accounts rendering the same job get two different
    # archive names.
    joined_a = "\n".join(cells_src(
        build([1], settings, "me/x", tmp_path / "a", "alice/render-job1")))
    joined_b = "\n".join(cells_src(
        build([1], settings, "me/x", tmp_path / "b", "bob/render-job1")))
    archive_line_a = next(l for l in joined_a.splitlines() if l.startswith("ARCHIVE ="))
    archive_line_b = next(l for l in joined_b.splitlines() if l.startswith("ARCHIVE ="))
    assert archive_line_a != archive_line_b
    assert "alice" in archive_line_a
    assert "bob" in archive_line_b


def test_archive_is_written_inside_the_frame_loop_not_only_at_the_end(tmp_path, settings):
    # THE non-negotiable from the brief: if the archive were written only
    # once after the whole FRAMES loop finishes, a session that hits the
    # wall or dies mid-render would lose every frame's inclusion in the
    # archive -- the archive write must happen as each frame completes, so
    # a partial render still produces a partial (not empty, not missing)
    # archive alongside the loose files.
    # Frames are now rendered by ONE Blender process, so "inside the loop"
    # means inside the per-frame callback the runner fires as each
    # PROGRESS line arrives -- not after the process exits. The guarantee
    # is unchanged: a kernel killed mid-render still leaves a partial
    # archive rather than none.
    cells = cells_src(build([1, 2, 3], settings, "me/x", tmp_path, "me/r"))
    runner = next(c for c in cells if "def render_frames" in c)
    cb_idx = runner.index("def on_frame(")
    archive_idx = runner.index("_archive_new(out_prefix, archive, archived)",
                               cb_idx)
    wait_idx = runner.index("rc, tail = _run_blender(")
    assert cb_idx < archive_idx < wait_idx, (
        "the archive write must happen in the per-frame callback, which "
        "fires while Blender is still running -- not after it exits")


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


def test_archive_only_includes_frames_that_actually_exist(tmp_path, settings):
    # A failed frame's (nonexistent) output must never be swept into the
    # archive. This used to be enforced by only zipping on ok; it is now
    # enforced by construction -- the archiver adds what it finds ON DISK,
    # and a frame that failed wrote no file. Each file is added once, so a
    # later frame's pass cannot re-add an earlier one.
    cells = cells_src(build([1], settings, "me/x", tmp_path, "me/r"))
    runner = next(c for c in cells if "def _archive_new" in c)
    assert "for path in sorted(glob.glob(out_prefix" in runner
    assert "if name in archived:" in runner
    assert "archived.add(name)" in runner


# --------------------------------------------------------------------------
# One process for the whole frame list.
#
# Measured on a live 15-frame render (2026-08-11): 35s per frame with both
# T4s busy only ~25% of the wall clock, because every frame paid Blender
# startup, GPU context creation and a fresh parse of the same 66 MB scene.
# --------------------------------------------------------------------------

def test_blender_is_launched_once_for_the_whole_frame_list(tmp_path, settings):
    joined = "\n".join(cells_src(build([1, 2, 3], settings, "me/x", tmp_path,
                                       "me/r")))
    # The old shape: a subprocess per frame, told which frame with -f.
    assert '"-f", str(frame)' not in joined, \
        "one Blender process per frame re-parses the scene every time"
    assert 'env["BR_FRAMES"] = ",".join(str(f) for f in frames)' in joined


def test_the_frame_loop_runs_inside_blender(tmp_path, settings):
    joined = "\n".join(cells_src(build([1, 2, 3], settings, "me/x", tmp_path,
                                       "me/r")))
    assert "for frame in FRAMES:" in joined
    assert "bpy.ops.render.render(write_still=True)" in joined
    # write_still writes filepath verbatim -- unlike -f it does NOT append
    # the frame number, so every frame would overwrite the last without this.
    assert 's.render.filepath = f"{OUT}{frame:04d}"' in joined


def test_a_failed_frame_does_not_abort_the_rest_of_the_batch(tmp_path,
                                                             settings):
    # The whole risk of batching: one bad frame taking every later frame
    # with it. The in-Blender loop must catch per-frame and carry on.
    joined = "\n".join(cells_src(build([1, 2, 3], settings, "me/x", tmp_path,
                                       "me/r")))
    setup = joined[joined.index("for frame in FRAMES:"):]
    assert "except Exception:" in setup
    assert "(done if ok else failed).append(frame)" in setup


def test_frames_the_batch_never_reported_are_retried_one_at_a_time(
        tmp_path, settings):
    # A segfault kills the process, not just its current frame -- Python's
    # try/except cannot catch that. Anything unreported falls back to the
    # old one-process-per-frame path, so the worst case is what batching
    # replaced, never worse.
    runner = next(c for c in cells_src(build([1, 2, 3], settings, "me/x",
                                             tmp_path, "me/r"))
                  if "def render_frames" in c)
    assert "remaining = [f for f in frames if f not in seen]" in runner
    assert "_run_blender([frame], blend, env, on_frame)" in runner


def test_progress_totals_come_from_the_runner_not_from_blender(tmp_path,
                                                               settings):
    # A fallback child renders ONE frame and so reports "done=1/1". Echoing
    # that would slam the app's progress bar to 100% on the first recovered
    # frame of a 15-frame job, so the runner re-emits every PROGRESS line
    # with the true total and never forwards Blender's own.
    runner = next(c for c in cells_src(build([1, 2, 3], settings, "me/x",
                                             tmp_path, "me/r"))
                  if "def render_frames" in c)
    assert "total = len(frames)" in runner
    assert "done={len(done)}/{total}" in runner
    echo = runner[runner.index("def _run_blender"):runner.index("def render_frames")]
    assert "print(line, flush=True)" in echo
    assert 'elif line.startswith("[setup]")' in echo, \
        "PROGRESS must be parsed and re-emitted, not echoed straight through"


def test_persistent_data_is_enabled_between_frames(tmp_path, settings):
    # Only means anything once consecutive frames share a process: it
    # keeps BVH and geometry resident instead of rebuilding per frame.
    joined = "\n".join(cells_src(build([1, 2], settings, "me/x", tmp_path,
                                       "me/r")))
    assert "s.render.use_persistent_data = True" in joined
    assert 'os.environ.get("BR_PERSISTENT", "1") == "1"' in joined


def test_the_one_time_setup_cost_is_measured_not_guessed(tmp_path, settings):
    # The reason for this whole change was inferred from GPU duty cycle.
    # The notebook now reports it directly, so the next run states the
    # startup+parse cost instead of implying it.
    joined = "\n".join(cells_src(build([1, 2], settings, "me/x", tmp_path,
                                       "me/r")))
    assert 'env["BR_T0"] = repr(time.time())' in joined
    assert "[setup] startup+scene load {_t_ready - _t_launch:.1f}s" in joined


# --------------------------------------------------------------------------
# Per-frame post-processing belongs on the idle GPUs.
#
# Measured on a real session (2026-08-12): the scene ships
# denoiser=OPENIMAGEDENOISE with denoising_use_gpu=False, and a Kaggle box
# pairs FOUR cpu cores with two Tesla T4s. Both GPUs read idle for ~77% of
# every 23s frame while those four cores denoised and composited it.
# --------------------------------------------------------------------------

def test_denoising_and_compositing_are_moved_to_the_gpu(tmp_path, settings):
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path,
                                       "me/r")))
    assert "s.cycles.denoising_use_gpu = True" in joined
    assert 's.render.compositor_device = "GPU"' in joined


def test_moving_post_processing_is_reported(tmp_path, settings):
    # The whole reason this was invisible for so long is that nobody could
    # see where the work ran. Both the setting and the outcome are logged.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path,
                                       "me/r")))
    assert "[setup] on GPU: " in joined


def test_a_missing_blender_attribute_costs_speed_not_the_render(tmp_path,
                                                               settings):
    # These attribute names have moved between Blender versions
    # (Scene.node_tree vanished in 5.2 and crashed a probe outright). A
    # version without them must render slowly, never fail.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path,
                                       "me/r")))
    block = joined[joined.index('if os.environ.get("BR_POST_GPU"'):]
    block = block[:block.index("FRAMES = [")]
    assert block.count("except (AttributeError, TypeError)") == 2, \
        "both assignments must be guarded independently"


def test_denoising_is_not_switched_on_for_a_scene_that_has_it_off(tmp_path,
                                                                 settings):
    # Moving the denoiser to the GPU must never mean ENABLING denoising:
    # that would change the image of a scene deliberately rendered raw.
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path,
                                       "me/r")))
    assert 'if getattr(s.cycles, "use_denoising", False):' in joined
    assert "s.cycles.use_denoising = True" not in joined


def test_post_on_gpu_can_be_turned_off(tmp_path):
    # It costs VRAM, so a scene bigger than the one measured can opt out.
    off = RenderSettings(1920, 1080, 64, "PNG", post_on_gpu=False)
    joined = "\n".join(cells_src(build([1], off, "me/x", tmp_path, "me/r")))
    assert 'POST_GPU = False' in joined
    assert '"BR_POST_GPU": "1" if POST_GPU else "0"' in joined


def test_post_on_gpu_defaults_to_on(tmp_path, settings):
    assert RenderSettings(1, 1, 1).post_on_gpu is True
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path,
                                       "me/r")))
    assert "POST_GPU = True" in joined


def test_warm_worker_also_gets_the_gpu_post_processing_flag(tmp_path,
                                                            settings):
    joined = "\n".join(cells_src(build(
        [1, 2], settings, "me/x", tmp_path, "me/r", mode="worker",
        control_slug="me/ctl", token="KGAT_x", worker_label="w1")))
    assert '"BR_POST_GPU": "1" if job.get("postGpu", POST_GPU) else "0"' \
        in joined


def test_warm_worker_uses_the_same_runner_and_progress_format(tmp_path,
                                                              settings):
    # The warm worker used to print "PROGRESS 3/9", which
    # log_stream.PROGRESS_RE (it requires frame=) never matched -- a warm
    # job's progress was invisible to the app.
    joined = "\n".join(cells_src(build(
        [1, 2], settings, "me/x", tmp_path, "me/r", mode="worker",
        control_slug="me/ctl", token="KGAT_x", worker_label="w1")))
    assert "render_frames(frames, blend, env," in joined
    assert "PROGRESS {len(done)}/{len(frames)}" not in joined


def test_every_cell_is_still_valid_python_with_archiving(tmp_path, settings):
    p = build([1, 2, 3], settings, "me/remember-blend", tmp_path, "me/render-0")
    for src in cells_src(p):
        ast.parse(src)


# ---------------------------------------------------------------------------
# Warm workers. A machine that comes up and waits is only useful if it can
# (a) hear about a job, and (b) stop itself when there is none -- the
# second matters more, because a session bills GPU quota by wall-clock and
# an app that crashes must not strand one waiting forever.
# ---------------------------------------------------------------------------

def _worker_source(tmp_path, **kwargs):
    options = dict(mode="worker", control_slug="me/blendfleet-control",
                   token="KGAT_" + "0" * 32, worker_label="acct0")
    options.update(kwargs)
    path = nb.build([], nb.RenderSettings(1920, 1080, 128), "me/scene-blend",
                    tmp_path / "w", "me/scene-worker-1", **options)
    cells = json.loads(path.read_text(encoding="utf-8"))["cells"]
    return "".join(cells[-1]["source"]), path


def test_worker_mode_refuses_without_a_way_to_hear_about_a_job(tmp_path):
    """Anything missing here produces a machine that starts, spends quota
    and waits forever. Refusing costs nothing; the notebook does not."""
    for missing in ("control_slug", "token", "worker_label"):
        with pytest.raises(ValueError):
            _worker_source(tmp_path, **{missing: None})


def test_worker_shuts_itself_down_when_idle(tmp_path):
    """The app is not what stops it. A laptop that sleeps or an app that
    crashes must not leave a session quietly eating a friend's quota."""
    source, _ = _worker_source(tmp_path)
    assert f"IDLE_TIMEOUT_S = {nb.IDLE_TIMEOUT_S}" in source
    assert "idle_for > IDLE_TIMEOUT_S" in source
    assert "break" in source


def test_worker_has_a_maximum_lifetime_as_well_as_an_idle_timeout(tmp_path):
    """A worker kept busy by a trickle of jobs would never hit the idle
    path. Eleven hours warm is a leak, not a warm machine."""
    source, _ = _worker_source(tmp_path)
    assert f"MAX_LIFETIME_S = {nb.MAX_WORKER_LIFETIME_S}" in source
    assert "time.time() - started > MAX_LIFETIME_S" in source


def test_the_idle_clock_restarts_after_work_not_before(tmp_path):
    """A job that took an hour must not count as an hour of idling and
    shut the machine down the moment it finishes."""
    source, _ = _worker_source(tmp_path)
    tail = source[source.index("DONE"):]
    assert "last_activity = time.time()" in tail


def test_a_control_dataset_that_is_unreachable_does_not_kill_the_worker(tmp_path):
    """A blip in the control channel means "no news", not "die"."""
    source, _ = _worker_source(tmp_path)
    assert "except Exception" in source
    assert "return None" in source


def test_the_control_dataset_is_never_attached(tmp_path):
    """An attached dataset is PINNED at session start, so a worker would
    never see a new version of it -- which is the entire mechanism. It has
    to be fetched over the API instead."""
    _, path = _worker_source(tmp_path)
    metadata = json.loads(
        (path.parent / "kernel-metadata.json").read_text(encoding="utf-8"))
    assert metadata["dataset_sources"] == ["me/scene-blend"]
    assert "me/blendfleet-control" not in metadata["dataset_sources"]


def test_a_worker_kernel_is_private(tmp_path):
    """It carries that account's own token. A public kernel would expose
    it, which is the whole risk this mode is opt-in for."""
    _, path = _worker_source(tmp_path)
    metadata = json.loads(
        (path.parent / "kernel-metadata.json").read_text(encoding="utf-8"))
    assert metadata["is_private"] is True


def test_the_token_is_never_printed(tmp_path):
    """It appears exactly once, being put into the environment."""
    token = "KGAT_" + "a" * 32
    source, _ = _worker_source(tmp_path, token=token)
    assert source.count(token) == 1
    assert 'KAGGLE_API_TOKEN"] = ' in source
    for line in source.splitlines():
        if token in line:
            assert "print" not in line


def test_worker_only_takes_a_job_addressed_to_it_or_to_everyone(tmp_path):
    source, _ = _worker_source(tmp_path)
    assert "WORKER_LABEL in (job.get(\"workers\") or [WORKER_LABEL])" in source


def test_the_notebook_downloads_the_version_it_was_given(tmp_path):
    from blendfleet.notebook_builder import RenderSettings, build
    settings = RenderSettings(64, 36, 1, "PNG", blender_version="4.2.9")
    joined = "\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r")))
    assert "blender-4.2.9-linux-x64.tar.xz" in joined
    assert "release/Blender4.2/" in joined, \
        "the release directory is major.minor, not the full version"


def test_an_unusable_version_is_refused_before_a_kernel_is_built(tmp_path):
    """A 404 inside a running session costs that session's startup and
    reads like a network fault rather than a typo."""
    import pytest
    from blendfleet.notebook_builder import RenderSettings, build
    settings = RenderSettings(64, 36, 1, "PNG", blender_version="latest")
    with pytest.raises(ValueError) as excinfo:
        build([1], settings, "me/x", tmp_path, "me/r")
    assert "major.minor.patch" in str(excinfo.value)


def test_setup_is_identical_between_render_and_worker_modes(tmp_path):
    """The thing that renders frames is the same render_setup.py either
    way -- a warm worker must not be able to drift into rendering
    differently from a one-shot job."""
    settings = nb.RenderSettings(1920, 1080, 128)
    render = nb.build([1, 2], settings, "me/scene-blend", tmp_path / "r",
                      "me/scene-render-1")
    worker = nb.build([], settings, "me/scene-blend", tmp_path / "w2",
                      "me/scene-worker-2", mode="worker",
                      control_slug="me/ctl", token="KGAT_" + "0" * 32,
                      worker_label="acct0")
    render_cells = json.loads(render.read_text(encoding="utf-8"))["cells"]
    worker_cells = json.loads(worker.read_text(encoding="utf-8"))["cells"]
    # Cells 1-3 (preflight, Blender, render_setup.py) and the telemetry
    # thread are shared; only the last cell differs.
    assert [c["source"] for c in render_cells[1:4]] == \
        [c["source"] for c in worker_cells[1:4]]
    assert render_cells[-1]["source"] != worker_cells[-1]["source"]
