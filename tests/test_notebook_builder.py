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
    assert joined.count("flush=True") == 1, "expected exactly one flush=True"
    idx_progress = joined.index('print(f"PROGRESS')
    idx_flush = joined.index("flush=True")
    # flush=True must belong to the PROGRESS print call itself, not some
    # unrelated statement elsewhere in the notebook.
    assert idx_progress < idx_flush < idx_progress + 200


def test_no_hardcoded_gpu_model_in_device_selection(tmp_path, settings):
    # GPU model is not guaranteed (a request for GPU returned a single P100,
    # not the T4 x2 that was expected) -- device selection must never assume
    # a specific card. (checked against code only; a prose comment
    # legitimately references the P100 incident that motivated this rule.)
    code = strip_comments("\n".join(cells_src(build([1], settings, "me/x", tmp_path, "me/r"))))
    for model in ("P100", "T4", "V100", "A100"):
        assert model not in code
