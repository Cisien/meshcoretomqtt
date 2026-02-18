"""Tier 5: Tests for real Docker build and container lifecycle.

Requires MCTOMQTT_TEST_DOCKER=1 and Docker daemon running.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from installer.system import docker_cmd

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).parent.parent
IMAGE_NAME = "mctomqtt-test"
CONTAINER_NAME = "mctomqtt-test"


@pytest.fixture(autouse=True)
def require_docker() -> None:
    if not os.environ.get("MCTOMQTT_TEST_DOCKER"):
        pytest.skip("Set MCTOMQTT_TEST_DOCKER=1 to run")


@pytest.fixture(autouse=True)
def cleanup_container() -> Generator[None, None, None]:
    """Remove test container and image after each test."""
    yield
    docker = docker_cmd() or "docker"
    subprocess.run(
        f"{docker} rm -f {CONTAINER_NAME}".split(),
        capture_output=True, check=False,
    )
    subprocess.run(
        f"{docker} rmi -f {IMAGE_NAME}:latest".split(),
        capture_output=True, check=False,
    )


class TestDocker:
    def test_docker_cmd_available(self) -> None:
        result = docker_cmd()
        assert result is not None, "Docker not available"
        assert "docker" in result

    def test_build_image(self) -> None:
        docker = docker_cmd()
        assert docker is not None
        result = subprocess.run(
            f"{docker} build -t {IMAGE_NAME}:latest {PROJECT_ROOT}".split(),
            capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, f"Build failed: {result.stderr}"

        # Verify image exists
        result = subprocess.run(
            f"{docker} images {IMAGE_NAME}:latest --format {{{{.Repository}}}}".split(),
            capture_output=True, text=True,
        )
        assert IMAGE_NAME in result.stdout

    def test_container_starts(self) -> None:
        docker = docker_cmd()
        assert docker is not None

        # Build first
        subprocess.run(
            f"{docker} build -t {IMAGE_NAME}:latest {PROJECT_ROOT}".split(),
            capture_output=True, check=True, timeout=300,
        )

        # Run container (it will likely fail without serial device, but should start)
        result = subprocess.run(
            f"{docker} run -d --name {CONTAINER_NAME} {IMAGE_NAME}:latest".split(),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Container start failed: {result.stderr}"

        # Verify container exists in ps
        result = subprocess.run(
            f"{docker} ps -a --filter name={CONTAINER_NAME} --format {{{{.Names}}}}".split(),
            capture_output=True, text=True,
        )
        assert CONTAINER_NAME in result.stdout
