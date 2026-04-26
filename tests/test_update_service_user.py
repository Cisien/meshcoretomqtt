"""Tests for service-user handling during updates."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from installer import InstallerContext
from installer.update_cmd import _do_update


def _write_repo_fixture(repo_dir: Path) -> None:
    repo_dir.mkdir()
    (repo_dir / "mctomqtt.py").write_text('__version__ = "1.2.0-test"\n')
    (repo_dir / "auth_token.py").write_text("")
    (repo_dir / "config_loader.py").write_text("")
    (repo_dir / "config.toml.example").write_text("[general]\niata = \"XXX\"\n")
    (repo_dir / "uninstall.sh").write_text("#!/bin/sh\n")
    (repo_dir / "mctomqtt.service").write_text(
        "[Service]\nUser=mctomqtt\nGroup=mctomqtt\n"
    )
    (repo_dir / "bridge").mkdir()
    (repo_dir / "bridge" / "__init__.py").write_text("")


def test_update_detects_service_user_before_reconfigure(tmp_path: Path) -> None:
    """Reconfigure path must not chown config using the default mctomqtt group."""
    repo_dir = tmp_path / "repo"
    install_dir = tmp_path / "opt" / "mctomqtt"
    config_dir = tmp_path / "etc" / "mctomqtt"
    config_d = config_dir / "config.d"
    work_dir = tmp_path / "work"
    _write_repo_fixture(repo_dir)
    install_dir.mkdir(parents=True)
    config_d.mkdir(parents=True)
    work_dir.mkdir()
    (install_dir / "mctomqtt.py").write_text('__version__ = "old"\n')
    (config_d / "00-user.toml").write_text('[[broker]]\nname = "old"\n')

    ctx = InstallerContext(
        install_dir=str(install_dir),
        config_dir=str(config_dir),
        local_install=str(repo_dir),
    )

    order: list[str] = []

    def fake_detect_service_user(_ctx: InstallerContext) -> str:
        order.append("detect_service_user")
        return "customsvc"

    def fake_create_system_user(user: str, _install_dir: str) -> None:
        order.append(f"create_system_user:{user}")

    def fake_configure_mqtt_brokers(config_ctx: InstallerContext) -> None:
        order.append(f"configure_mqtt_brokers:{config_ctx.svc_user}")

    with (
        patch("installer.update_cmd.detect_system_type", return_value="manual"),
        patch("installer.update_cmd.detect_service_user", side_effect=fake_detect_service_user),
        patch("installer.update_cmd.create_system_user", side_effect=fake_create_system_user),
        patch("installer.update_cmd.configure_mqtt_brokers", side_effect=fake_configure_mqtt_brokers),
        patch("installer.update_cmd.create_venv"),
        patch("installer.update_cmd.cleanup_legacy_nvm"),
        patch("installer.update_cmd.set_permissions"),
        patch("installer.update_cmd.create_version_info"),
        patch("installer.update_cmd.prompt_yes_no", return_value=True),
        patch(
            "installer.update_cmd.run_cmd",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as run_cmd,
    ):
        _do_update(ctx, str(work_dir))

    run_cmd.assert_any_call(
        ["python3", "-m", "py_compile", str(work_dir / "mctomqtt.py")],
        check=False,
        capture=True,
    )
    assert order[:3] == [
        "detect_service_user",
        "create_system_user:customsvc",
        "configure_mqtt_brokers:customsvc",
    ]
    assert ctx.svc_user == "customsvc"
