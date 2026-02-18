"""Tier 4: Tests for installer.system.create_system_user (Linux only)."""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Generator

import pytest

from installer.system import create_system_user

pytestmark = [
    pytest.mark.system,
    pytest.mark.skipif(platform.system() != "Linux", reason="Linux only"),
]

TEST_USER = "mctomqtt_test"
TEST_DIR = "/tmp/mctomqtt_test"


@pytest.fixture(autouse=True)
def cleanup_test_user() -> Generator[None, None, None]:
    """Ensure test user is removed after each test."""
    yield
    subprocess.run(
        ["userdel", TEST_USER],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["rm", "-rf", TEST_DIR],
        capture_output=True, check=False,
    )


class TestCreateSystemUser:
    def test_creates_user(self) -> None:
        subprocess.run(["mkdir", "-p", TEST_DIR], check=True)
        create_system_user(TEST_USER, TEST_DIR)

        result = subprocess.run(
            ["id", TEST_USER], capture_output=True, text=True,
        )
        assert result.returncode == 0

    def test_nologin_shell(self) -> None:
        subprocess.run(["mkdir", "-p", TEST_DIR], check=True)
        create_system_user(TEST_USER, TEST_DIR)

        result = subprocess.run(
            ["getent", "passwd", TEST_USER],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        # Last field is the shell
        shell = result.stdout.strip().split(":")[-1]
        assert "nologin" in shell

    def test_serial_group_membership(self) -> None:
        subprocess.run(["mkdir", "-p", TEST_DIR], check=True)
        create_system_user(TEST_USER, TEST_DIR)

        result = subprocess.run(
            ["groups", TEST_USER], capture_output=True, text=True,
        )
        # Should be in either dialout or uucp depending on distro
        groups_str = result.stdout.lower()
        assert "dialout" in groups_str or "uucp" in groups_str

    def test_idempotent(self, capsys: pytest.CaptureFixture[str]) -> None:
        subprocess.run(["mkdir", "-p", TEST_DIR], check=True)
        create_system_user(TEST_USER, TEST_DIR)
        # Second call should succeed without error
        create_system_user(TEST_USER, TEST_DIR)
        captured = capsys.readouterr()
        assert "already exists" in captured.out.lower()
