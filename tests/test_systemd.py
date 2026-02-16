"""Tier 5: Tests for real systemd service lifecycle.

Requires MCTOMQTT_TEST_SYSTEMD=1 and sudo access.
Uses a test unit name (mctomqtt-test.service) to avoid clobbering production.
"""

import os
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.e2e

SERVICE_NAME = "mctomqtt-test"
UNIT_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"


@pytest.fixture(autouse=True)
def require_systemd():
    if not os.environ.get("MCTOMQTT_TEST_SYSTEMD"):
        pytest.skip("Set MCTOMQTT_TEST_SYSTEMD=1 to run")


@pytest.fixture(autouse=True)
def cleanup_service():
    """Ensure test service is removed after each test."""
    yield
    subprocess.run(
        ["sudo", "systemctl", "stop", f"{SERVICE_NAME}.service"],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["sudo", "systemctl", "disable", f"{SERVICE_NAME}.service"],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["sudo", "rm", "-f", UNIT_PATH],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["sudo", "systemctl", "daemon-reload"],
        capture_output=True, check=False,
    )


def _install_test_unit():
    """Install a minimal test systemd unit that just sleeps."""
    unit_content = f"""[Unit]
Description=MeshCore to MQTT Test Service

[Service]
Type=exec
ExecStart=/bin/sleep infinity
Restart=always

[Install]
WantedBy=multi-user.target
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".service", delete=False) as f:
        f.write(unit_content)
        tmp_path = f.name

    subprocess.run(["sudo", "cp", tmp_path, UNIT_PATH], check=True)
    os.unlink(tmp_path)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)


class TestSystemdService:
    def test_install_and_enable(self):
        _install_test_unit()
        subprocess.run(
            ["sudo", "systemctl", "enable", f"{SERVICE_NAME}.service"],
            check=True,
        )

        result = subprocess.run(
            ["systemctl", "is-enabled", f"{SERVICE_NAME}.service"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "enabled"

    def test_start_and_active(self):
        _install_test_unit()
        subprocess.run(
            ["sudo", "systemctl", "enable", "--now", f"{SERVICE_NAME}.service"],
            check=True,
        )

        result = subprocess.run(
            ["systemctl", "is-active", f"{SERVICE_NAME}.service"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "active"

    def test_stop_and_disable_clean(self):
        _install_test_unit()
        subprocess.run(
            ["sudo", "systemctl", "enable", "--now", f"{SERVICE_NAME}.service"],
            check=True,
        )
        subprocess.run(
            ["sudo", "systemctl", "stop", f"{SERVICE_NAME}.service"],
            check=True,
        )
        subprocess.run(
            ["sudo", "systemctl", "disable", f"{SERVICE_NAME}.service"],
            check=True,
        )

        result = subprocess.run(
            ["systemctl", "is-active", f"{SERVICE_NAME}.service"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() != "active"
