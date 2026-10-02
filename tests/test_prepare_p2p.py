"""Regression checks for the P2P prep script (no network or /etc writes)."""

import importlib.util
from pathlib import Path
import shutil

import pytest

import repo

SCRIPT = repo.ROOT / "prepare-p2p.py"


def load_script():
    spec = importlib.util.spec_from_file_location("prepare_p2p", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_tree(tmp_path, monkeypatch):
    prep = load_script()
    for file in ("driver/build.sh", "common/constants.yaml", "install.sh"):
        target = tmp_path / file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo.ROOT / file, target)
    # Start from the unprepared layout even when the working tree is prepared.
    build = tmp_path / "driver/build.sh"
    constants = tmp_path / "common/constants.yaml"
    install = tmp_path / "install.sh"
    for key, name in prep.P2P_PATCHES:
        build.write_text(build.read_text().replace(f"    {name}\n", ""))
        constants.write_text(constants.read_text().replace(
            f"\n  {key}:\n    patch: {name}\n    registers: {{}}\n", ""
        ))
    install.write_text(install.read_text().replace(prep.REGKEY_P2P, prep.REGKEY_BASE))
    patch_dir = tmp_path / "driver/patches"
    patch_dir.mkdir()
    monkeypatch.setattr(prep, "PATCH_DIR", patch_dir)
    monkeypatch.setattr(prep, "BUILD", tmp_path / "driver/build.sh")
    monkeypatch.setattr(prep, "CONSTANTS", tmp_path / "common/constants.yaml")
    monkeypatch.setattr(prep, "INSTALL", tmp_path / "install.sh")
    monkeypatch.setattr(prep, "download_patch", lambda name: f"patch data for {name}\n".encode())
    return prep


def test_prep_updates_installer_and_is_idempotent(tmp_path, monkeypatch, capsys):
    prep = fixture_tree(tmp_path, monkeypatch)
    prep.main()
    output = capsys.readouterr().out
    assert "re-run ./install.sh" in output
    assert "then reboot" in output
    install = prep.INSTALL.read_text()
    assert prep.REGKEY_P2P + "\n" in install
    assert prep.REGKEY_BASE + "\n" not in install
    assert "cat > /etc/modprobe.d/cmp-pcie-gen2.conf <<'EOF'" in install
    assert all((prep.PATCH_DIR / name).exists() for _, name in prep.P2P_PATCHES)
    assert all(name in prep.BUILD.read_text() for _, name in prep.P2P_PATCHES)
    assert all(key + ":" in prep.CONSTANTS.read_text() for key, _ in prep.P2P_PATCHES)

    expected = {path: path.read_bytes() for path in (prep.BUILD, prep.CONSTANTS, prep.INSTALL)}
    prep.main()
    assert all(path.read_bytes() == content for path, content in expected.items())


def test_unexpected_installer_regkey_fails_before_writing(tmp_path, monkeypatch):
    prep = fixture_tree(tmp_path, monkeypatch)
    prep.INSTALL.write_text(prep.INSTALL.read_text().replace(prep.REGKEY_BASE, "custom regkey"))
    with pytest.raises(ValueError, match="Unexpected PCIe Gen2 modprobe contents"):
        prep.main()
    assert not list(prep.PATCH_DIR.iterdir())
    assert "0011-p2p-bar1.patch" not in prep.BUILD.read_text()
    assert "p2p_bar1:" not in prep.CONSTANTS.read_text()
