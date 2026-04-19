import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json
import sys
import os
import subprocess
import shutil
import platform

from installer.system import (
    detect_linux_distro,
    check_piwheels_available,
    install_os_build_deps
)

# Sample piwheels response for the ed25519-orlp package
PIWHEELS_JSON_RAW = """
{
  "package": "ed25519-orlp",
  "releases": {
    "0.1.1": {
      "files": {
        "ed25519_orlp-0.1.1-cp313-cp313-linux_armv6l.whl": {
          "file_abi_tag": "cp313",
          "platform": "linux_armv6l"
        },
        "ed25519_orlp-0.1.1-cp311-cp311-linux_armv7l.whl": {
          "file_abi_tag": "cp311",
          "platform": "linux_armv7l"
        }
      }
    }
  }
}
"""

OS_RELEASE_SAMPLES = {
    "debian": 'ID=debian\nPRETTY_NAME="Debian GNU/Linux 13"',
    "ubuntu": 'ID=ubuntu\nPRETTY_NAME="Ubuntu 24.04"\nID_LIKE=debian',
    "raspbian": 'ID=raspbian\nID_LIKE=debian',
    "arch": 'ID=arch\nPRETTY_NAME="Arch Linux"',
    "fedora": 'ID=fedora\nVERSION_ID=43',
    "centos": 'ID="centos"\nID_LIKE="rhel fedora"',
    "alpine": 'ID=alpine\nNAME="Alpine Linux"',
    "gentoo": 'NAME=Gentoo\nID=gentoo\nPRETTY_NAME="Gentoo Linux"',
}

class TestSystemDeps:

    @pytest.mark.parametrize("expected_id, content, expected_like", [
        ("debian", 'ID=debian', None),
        ("ubuntu", 'ID=ubuntu\nID_LIKE=debian', "debian"),
        ("linuxmint", 'ID=linuxmint\nID_LIKE="ubuntu debian"', "ubuntu debian"),
    ])
    def test_detect_linux_distro(self, expected_id, content, expected_like):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=content):
            dist_id, dist_like = detect_linux_distro()
            assert dist_id == expected_id
            assert dist_like == expected_like

    def test_detect_linux_distro_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            assert detect_linux_distro() == (None, None)

    @patch("installer.system.http_get")
    @patch("platform.uname")
    @patch("sys.version_info")
    def test_check_piwheels_available_success(self, mock_ver, mock_uname, mock_http):
        mock_http.return_value = PIWHEELS_JSON_RAW.encode()

        # Mock ARMv7l Pi running Python 3.11
        mock_uname.return_value = MagicMock(system="Linux", machine="armv7l")
        mock_ver.major = 3
        mock_ver.minor = 11

        assert check_piwheels_available("ed25519-orlp") is True

    @patch("shutil.which")
    def test_install_os_build_deps_already_present(self, mock_which):
        # Both cc and make present
        mock_which.side_effect = lambda x: "/usr/bin/" + x

        with patch("installer.system.print_success") as mock_print:
            assert install_os_build_deps() is True
            mock_print.assert_any_call("Build tools (C compiler and make) are already installed")

    @patch("shutil.which", return_value=None)
    @patch("installer.system.check_piwheels_available", return_value=True)
    @patch("installer.system.prompt_yes_no", return_value=False)
    def test_install_os_build_deps_piwheels_skip(self, mock_prompt, mock_pi, mock_which):
        assert install_os_build_deps() is True
        mock_prompt.assert_called_with("Install build tools anyway?", "n")

    @patch("shutil.which", return_value=None)
    @patch("installer.system.check_piwheels_available", return_value=False)
    @patch("installer.system.detect_linux_distro", return_value=("debian", None))
    @patch("installer.system.prompt_yes_no", return_value=True)
    @patch("installer.system.run_cmd")
    def test_install_os_build_deps_debian_install(self, mock_run, mock_prompt, mock_distro, mock_pi, mock_which):
        mock_run.return_value = MagicMock(returncode=0)

        assert install_os_build_deps() is True

        # Should call apt-get update and apt-get install
        mock_run.assert_any_call(["apt-get", "update", "-qq"], check=True)
        mock_run.assert_any_call(["apt-get", "install", "-y", "-qq", "build-essential", "python3-dev"], check=True)

    @patch("shutil.which", return_value=None)
    @patch("installer.system.check_piwheels_available", return_value=False)
    @patch("installer.system.detect_linux_distro", return_value=("linuxmint", "ubuntu debian"))
    @patch("installer.system.prompt_yes_no", return_value=True)
    @patch("installer.system.run_cmd")
    def test_install_os_build_deps_id_like_fallback(self, mock_run, mock_prompt, mock_distro, mock_pi, mock_which):
        mock_run.return_value = MagicMock(returncode=0)

        # Mint (ubuntu debian) should fallback to ubuntu (or debian)
        assert install_os_build_deps() is True

        # Should call apt-get (since ubuntu/debian use it)
        mock_run.assert_any_call(["apt-get", "update", "-qq"], check=True)

    @patch("shutil.which", return_value=None)
    @patch("installer.system.check_piwheels_available", return_value=False)
    @patch("installer.system.detect_linux_distro", return_value=("alpine", None))
    @patch("installer.system.prompt_yes_no", return_value=True)
    @patch("installer.system.run_cmd")
    def test_install_os_build_deps_alpine_install(self, mock_run, mock_prompt, mock_distro, mock_pi, mock_which):
        mock_run.return_value = MagicMock(returncode=0)

        assert install_os_build_deps() is True

        # Should call apk add
        mock_run.assert_called_with(["apk", "add", "--no-cache", "build-base", "python3-dev", "-y"], check=True)

    @patch("shutil.which", return_value=None)
    @patch("installer.system.detect_linux_distro", return_value=("gentoo", None))
    @patch("platform.system", return_value="Linux")
    def test_install_os_build_deps_unrecognized_linux(self, mock_sys, mock_distro, mock_which):
        with patch("installer.system.print_warning") as mock_warn:
            assert install_os_build_deps() is False
            mock_warn.assert_any_call("Unsupported or unrecognized distribution: gentoo (like: none)")

    @patch("shutil.which", return_value=None)
    @patch("installer.system.detect_linux_distro", return_value=(None, None))
    @patch("platform.system", return_value="Darwin")
    def test_install_os_build_deps_non_linux(self, mock_sys, mock_distro, mock_which):
        # On non-linux unknown distro, we just advise manual install
        with patch("installer.system.print_info") as mock_info:
            assert install_os_build_deps() is False
            mock_info.assert_any_call("Please manually install a C toolchain and Python headers if the installation fails.")
