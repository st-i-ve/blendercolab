from pathlib import Path
import blendfleet.platform_paths as pp


def test_windows_uses_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert pp.config_dir() == tmp_path / "BlendFleet"


def test_linux_respects_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert pp.config_dir() == tmp_path / "blendfleet"


def test_linux_defaults_without_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pp.config_dir() == tmp_path / ".config" / "blendfleet"


def test_dirs_are_created(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert pp.config_dir().is_dir()
    assert pp.state_dir().is_dir()
    assert pp.cache_dir().is_dir()
    assert pp.log_dir().is_dir()


def test_log_dir_sits_beside_state_and_cache(tmp_path, monkeypatch):
    """Crash logs live under the user's own data directory, never inside
    the PyInstaller bundle -- the bundle's temp directory is deleted by
    the very process exit a crash log has to outlive."""
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert pp.log_dir() == tmp_path / "blendfleet" / "logs"
