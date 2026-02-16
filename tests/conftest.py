"""Shared fixtures, pytest markers, and env-var-based skip logic."""

import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: needs internet access (skip with MCTOMQTT_SKIP_NETWORK=1)"
    )
    config.addinivalue_line(
        "markers", "system: needs root/Linux (auto-skipped when not root, or MCTOMQTT_SKIP_SYSTEM=1)"
    )
    config.addinivalue_line(
        "markers", "e2e: needs real services/devices (MCTOMQTT_TEST_E2E=1)"
    )


def pytest_collection_modifyitems(config, items):
    # network and system run by default; set SKIP vars to disable
    # e2e is opt-in; set MCTOMQTT_TEST_E2E=1 to enable
    for item in items:
        if "network" in item.keywords and os.environ.get("MCTOMQTT_SKIP_NETWORK"):
            item.add_marker(
                pytest.mark.skip(reason="MCTOMQTT_SKIP_NETWORK is set")
            )
        if "system" in item.keywords:
            if os.environ.get("MCTOMQTT_SKIP_SYSTEM"):
                item.add_marker(
                    pytest.mark.skip(reason="MCTOMQTT_SKIP_SYSTEM is set")
                )
            elif os.getuid() != 0:
                item.add_marker(
                    pytest.mark.skip(reason="system tests require root")
                )
        if "e2e" in item.keywords and not os.environ.get("MCTOMQTT_TEST_E2E"):
            item.add_marker(
                pytest.mark.skip(reason="Set MCTOMQTT_TEST_E2E=1 to run")
            )
