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
