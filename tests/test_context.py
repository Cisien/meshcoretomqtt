"""Tier 1: Tests for InstallerContext dataclass."""

from __future__ import annotations

from installer import InstallerContext


class TestInstallerContext:
    def test_default_values(self) -> None:
        ctx = InstallerContext()
        assert ctx.repo == "Cisien/meshcoretomqtt"
        assert ctx.branch == "main"
        assert ctx.install_dir == "/opt/mctomqtt"
        assert ctx.config_dir == "/etc/mctomqtt"
        assert ctx.svc_user == "mctomqtt"
        assert ctx.script_version == "unknown"
        assert ctx.decoder_available is False
        assert ctx.install_method == ""
        assert ctx.local_install == ""
        assert ctx.config_url == ""
        assert ctx.update_mode is False

    def test_post_init_sets_base_url(self) -> None:
        ctx = InstallerContext()
        assert ctx.base_url == "https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main"

    def test_custom_repo_branch_base_url(self) -> None:
        ctx = InstallerContext(repo="user/repo", branch="develop")
        assert ctx.base_url == "https://raw.githubusercontent.com/user/repo/develop"

    def test_custom_base_url_overwritten_by_post_init(self) -> None:
        # __post_init__ always sets base_url from repo/branch
        ctx = InstallerContext(repo="user/repo", branch="main", base_url="ignored")
        assert ctx.base_url == "https://raw.githubusercontent.com/user/repo/main"
