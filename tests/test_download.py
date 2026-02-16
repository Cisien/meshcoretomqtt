"""Tier 3: Tests for installer.system download functions with real curl to GitHub."""

import subprocess
from pathlib import Path

import pytest

from installer.system import download_file, download_repo_archive

pytestmark = pytest.mark.network


class TestDownloadFile:
    def test_download_known_file(self, tmp_path):
        dest = str(tmp_path / "README.md")
        download_file(
            "https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/README.md",
            dest,
            "README.md",
        )
        content = (tmp_path / "README.md").read_text()
        assert len(content) > 0
        assert "meshcoretomqtt" in content.lower()

    def test_invalid_url_raises(self, tmp_path):
        dest = str(tmp_path / "nonexistent")
        with pytest.raises(subprocess.CalledProcessError):
            download_file(
                "https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/DOES_NOT_EXIST_12345",
                dest,
                "nonexistent",
            )


class TestDownloadRepoArchive:
    def test_download_and_extract(self, tmp_path):
        repo_dir = download_repo_archive("Cisien/meshcoretomqtt", "main", str(tmp_path))
        repo_path = Path(repo_dir)
        assert repo_path.is_dir()
        assert (repo_path / "mctomqtt.py").exists()
        assert (repo_path / "auth_token.py").exists()
        assert (repo_path / "README.md").exists()

    def test_invalid_branch_raises(self, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            download_repo_archive("Cisien/meshcoretomqtt", "nonexistent-branch-12345", str(tmp_path))
