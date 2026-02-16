"""Tests for installer.system download functions."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from installer.system import download_file, download_repo_archive


class TestRepoArchiveSlashBranch:
    """Verify download_repo_archive handles branch names containing slashes."""

    def _make_fake_archive(self, tmp_path: Path, dir_name: str) -> None:
        """Create a zip that mimics GitHub's archive layout."""
        repo_root = tmp_path / "build" / dir_name
        repo_root.mkdir(parents=True)
        (repo_root / "mctomqtt.py").write_text('__version__ = "0.0.0"')
        zip_path = tmp_path / "work" / "repo.zip"
        zip_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in repo_root.rglob("*"):
                zf.write(f, f.relative_to(tmp_path / "build"))

    def test_slash_branch_resolves_directory(self, tmp_path: Path) -> None:
        """Branch 'cisien/overhaul' extracts to 'meshcoretomqtt-cisien-overhaul'."""
        self._make_fake_archive(tmp_path, "meshcoretomqtt-cisien-overhaul")
        work_dir = tmp_path / "work"

        with patch("installer.system.run_cmd") as mock_cmd, \
             patch("installer.system.print_info"), \
             patch("installer.system.print_success"):
            # Simulate curl writing the zip (already created above)
            mock_cmd.return_value = None
            result = download_repo_archive(
                "Cisien/meshcoretomqtt", "cisien/overhaul", str(work_dir),
            )

        assert Path(result).name == "meshcoretomqtt-cisien-overhaul"
        assert (Path(result) / "mctomqtt.py").exists()

    def test_simple_branch_still_works(self, tmp_path: Path) -> None:
        """Branch 'main' extracts to 'meshcoretomqtt-main' (no slashes)."""
        self._make_fake_archive(tmp_path, "meshcoretomqtt-main")
        work_dir = tmp_path / "work"

        with patch("installer.system.run_cmd") as mock_cmd, \
             patch("installer.system.print_info"), \
             patch("installer.system.print_success"):
            mock_cmd.return_value = None
            result = download_repo_archive(
                "Cisien/meshcoretomqtt", "main", str(work_dir),
            )

        assert Path(result).name == "meshcoretomqtt-main"


@pytest.mark.network
class TestDownloadFile:
    def test_download_known_file(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "README.md")
        download_file(
            "https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/README.md",
            dest,
            "README.md",
        )
        content = (tmp_path / "README.md").read_text()
        assert len(content) > 0
        assert "meshcoretomqtt" in content.lower()

    def test_invalid_url_raises(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "nonexistent")
        with pytest.raises(subprocess.CalledProcessError):
            download_file(
                "https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/DOES_NOT_EXIST_12345",
                dest,
                "nonexistent",
            )


@pytest.mark.network
class TestDownloadRepoArchive:
    def test_download_and_extract(self, tmp_path: Path) -> None:
        repo_dir = download_repo_archive("Cisien/meshcoretomqtt", "main", str(tmp_path))
        repo_path = Path(repo_dir)
        assert repo_path.is_dir()
        assert (repo_path / "mctomqtt.py").exists()
        assert (repo_path / "auth_token.py").exists()
        assert (repo_path / "README.md").exists()

    def test_invalid_branch_raises(self, tmp_path: Path) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            download_repo_archive("Cisien/meshcoretomqtt", "nonexistent-branch-12345", str(tmp_path))
