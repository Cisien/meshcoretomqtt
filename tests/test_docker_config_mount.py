"""Tests for Docker service command construction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from installer import InstallerContext
from installer.system import install_docker_service


def test_docker_service_mounts_config_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "etc" / "mctomqtt"
    config_d = config_dir / "config.d"
    config_d.mkdir(parents=True)
    (config_dir / "config.toml").write_text("[general]\n")
    (config_d / "99-user.toml").write_text('[serial]\nports = ["/dev/ttyACM0"]\n')

    ctx = InstallerContext(config_dir=str(config_dir), install_dir=str(tmp_path / "opt"))
    run_calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        run_calls.append(cmd)
        if cmd == ["docker", "--version"]:
            return SimpleNamespace(returncode=0, stdout="Docker version test\n")
        if cmd == ["docker", "ps", "-a"]:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch("installer.system.shutil.which", return_value="/usr/bin/docker"),
        patch("installer.system.docker_cmd", return_value="docker"),
        patch("installer.system.pull_or_build_docker_image", return_value="mctomqtt:test"),
        patch("installer.system.prompt_yes_no", return_value=True),
        patch("installer.system.run_cmd", side_effect=fake_run_cmd),
        patch("installer.system.check_service_health"),
        patch("installer.system.Path.exists", return_value=True),
    ):
        assert install_docker_service(ctx) is True

    docker_run = next(call for call in run_calls if call[:2] == ["docker", "run"])
    assert "-v" in docker_run
    assert f"{config_dir}:/etc/mctomqtt:ro" in docker_run
    assert not any("/config.toml:/etc/mctomqtt/config.toml" in part for part in docker_run)
    assert not any("/config.d/" in part and ":/etc/mctomqtt/config.d/" in part for part in docker_run)
