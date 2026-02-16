"""MeshCore to MQTT installer package."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InstallerContext:
    """Shared state passed between installer modules."""

    repo: str = "Cisien/meshcoretomqtt"
    branch: str = "main"
    install_dir: str = "/opt/mctomqtt"
    config_dir: str = "/etc/mctomqtt"
    svc_user: str = "mctomqtt"
    script_version: str = "unknown"
    decoder_available: bool = False
    install_method: str = ""  # "1" service, "2" docker, "3" manual
    local_install: str = ""  # LOCAL_INSTALL env var
    config_url: str = ""
    update_mode: bool = False
    base_url: str = ""
    repo_dir: str = ""  # path to extracted repo archive

    def __post_init__(self) -> None:
        self.base_url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}"
