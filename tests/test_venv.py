"""Tier 4: Tests for installer.system.create_venv with real venv creation."""

import subprocess

import pytest

from installer.system import create_venv

pytestmark = pytest.mark.system


class TestCreateVenv:
    def test_creates_venv(self, tmp_path):
        install_dir = str(tmp_path / "mctomqtt")
        (tmp_path / "mctomqtt").mkdir()
        create_venv(install_dir, "")

        venv_python = tmp_path / "mctomqtt" / "venv" / "bin" / "python3"
        assert venv_python.exists()

    def test_venv_has_dependencies(self, tmp_path):
        install_dir = str(tmp_path / "mctomqtt")
        (tmp_path / "mctomqtt").mkdir()
        create_venv(install_dir, "")

        venv_python = str(tmp_path / "mctomqtt" / "venv" / "bin" / "python3")
        result = subprocess.run(
            [venv_python, "-c", "import serial, paho.mqtt.client"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"

    def test_idempotent(self, tmp_path, capsys):
        install_dir = str(tmp_path / "mctomqtt")
        (tmp_path / "mctomqtt").mkdir()
        create_venv(install_dir, "")

        # Second call should detect existing venv
        create_venv(install_dir, "")
        captured = capsys.readouterr()
        assert "existing virtual environment" in captured.out.lower()
