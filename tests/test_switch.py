"""The switch to the CascadiaMesh version: [update] source and broker preset."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from installer import switch_cmd
from installer.config import validate_preset_toml

REPO, BRANCH = switch_cmd.CASCADIAMESH_REPO, switch_cmd.CASCADIAMESH_BRANCH
PRESET = Path(__file__).resolve().parent.parent / "presets" / "cascadiamesh.toml"


def _update(text: str) -> dict:
    return tomllib.loads(text)["update"]


def test_appends_update_when_missing():
    text = '[general]\niata = "SEA"\n'
    out = switch_cmd.set_update_source(text, REPO, BRANCH)
    assert _update(out) == {"repo": REPO, "branch": BRANCH}
    assert tomllib.loads(out)["general"]["iata"] == "SEA"


def test_changes_existing_update_lines_and_keeps_the_rest():
    text = (
        '# my config\n[general]\niata = "SEA"\n\n[update]\n# where updates come from\n'
        'repo = "Cisien/meshcoretomqtt"\nbranch = "main"\n\n[[broker]]\nname = "x"\nserver = "y"\n'
    )
    out = switch_cmd.set_update_source(text, REPO, BRANCH)
    assert out.count("[update]") == 1, "never a second [update] table"
    assert _update(out) == {"repo": REPO, "branch": BRANCH}
    assert "# where updates come from" in out and "# my config" in out
    assert tomllib.loads(out)["broker"][0]["server"] == "y"


def test_adds_a_missing_line_to_an_existing_update():
    out = switch_cmd.set_update_source('[update]\nbranch = "dev"\n', REPO, BRANCH)
    assert _update(out) == {"repo": REPO, "branch": BRANCH}


def test_empty_config():
    assert _update(switch_cmd.set_update_source("", REPO, BRANCH)) == {"repo": REPO, "branch": BRANCH}


def test_preset_is_valid_and_points_at_cascadiamesh():
    data = validate_preset_toml(PRESET)
    b = data["broker"][0]
    assert b["server"] == switch_cmd.CASCADIAMESH_SERVER == b["auth"]["audience"]
    assert b["auth"]["method"] == "token" and b["port"] == 443 and b["transport"] == "websockets"


def test_switch_config_adds_the_broker_once(tmp_path):
    (tmp_path / "config.d").mkdir()
    legacy = tmp_path / "config.d" / "00-user.toml"
    legacy.write_text('[general]\niata = "SEA"\n\n[update]\nrepo = "Cisien/meshcoretomqtt"\nbranch = "main"\n')
    done = switch_cmd.switch_config(tmp_path, REPO, BRANCH, PRESET.read_text())
    assert done["user_toml"].name == "99-user.toml" and not legacy.exists()
    assert _update(done["user_toml"].read_text()) == {"repo": REPO, "branch": BRANCH}
    assert done["preset"] is not None and done["preset"].name.endswith("cascadiamesh.toml")
    assert switch_cmd.has_cascadiamesh_broker(tmp_path)
    again = switch_cmd.switch_config(tmp_path, REPO, BRANCH, PRESET.read_text())
    assert again["preset"] is None, "already configured"


def test_an_existing_custom_cascadiamesh_broker_counts(tmp_path):
    (tmp_path / "config.d").mkdir()
    (tmp_path / "config.d" / "99-user.toml").write_text(
        '[[broker]]\nname = "custom-1"\nserver = "mqtt-v1.cascadiamesh.org"\n'
    )
    done = switch_cmd.switch_config(tmp_path, REPO, BRANCH, PRESET.read_text())
    assert done["preset"] is None


def test_no_broker(tmp_path):
    done = switch_cmd.switch_config(tmp_path, REPO, BRANCH, None)
    assert done["preset"] is None and not switch_cmd.has_cascadiamesh_broker(tmp_path)


def test_switch_script_defaults_to_this_fork():
    sh = (Path(__file__).resolve().parent.parent / "scripts" / "switch-to-cascadiamesh.sh").read_text()
    assert f'REPO="${{MCTOMQTT_REPO:-{REPO}}}"' in sh and f'BRANCH="${{MCTOMQTT_BRANCH:-{BRANCH}}}"' in sh
    assert "python3 -m installer switch" in sh


def test_invalid_toml_is_never_written(tmp_path, monkeypatch):
    (tmp_path / "config.d").mkdir()
    user = tmp_path / "config.d" / "99-user.toml"
    user.write_text('[update]\nrepo = "a"\n')
    monkeypatch.setattr(switch_cmd, "set_update_source", lambda *a: "[update\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        switch_cmd.switch_config(tmp_path, REPO, BRANCH, None)
    assert user.read_text() == '[update]\nrepo = "a"\n'
