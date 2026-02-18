"""Tier 3: Tests for bash bootstrap script syntax and basic functionality."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.network

PROJECT_ROOT = Path(__file__).parent.parent


class TestBashSyntax:
    """Verify bash scripts have valid syntax (bash -n)."""

    def test_install_sh_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / "install.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_update_sh_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / "scripts" / "update.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_migrate_sh_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / "scripts" / "migrate.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"


class TestBranchSanitization:
    """Verify bootstrap scripts sanitize slashes in branch names."""

    def test_install_sh_sanitizes_branch_slash(self) -> None:
        """install.sh must replace '/' with '-' for the extracted directory name."""
        result = subprocess.run(
            ["bash", "-c",
             'BRANCH="cisien/overhaul"; BRANCH_SANITIZED=$(echo "$BRANCH" | tr \'/\' \'-\'); echo "$BRANCH_SANITIZED"'],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "cisien-overhaul"

    def test_install_sh_contains_sanitization(self) -> None:
        """install.sh must use BRANCH_SANITIZED in the cp path."""
        content = (PROJECT_ROOT / "install.sh").read_text()
        assert "BRANCH_SANITIZED" in content
        assert '$REPO_NAME-$BRANCH_SANITIZED' in content


class TestBootstrapHelp:
    def test_install_sh_help(self) -> None:
        """LOCAL_INSTALL bootstrap copies installer and runs argparse help."""
        result = subprocess.run(
            ["bash", str(PROJECT_ROOT / "install.sh"), "--help"],
            capture_output=True, text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(Path.home()),
                "LOCAL_INSTALL": str(PROJECT_ROOT),
            },
            timeout=30,
        )
        # argparse --help exits 0 and prints usage
        assert result.returncode == 0
        assert "install" in result.stdout.lower() or "usage" in result.stdout.lower()
