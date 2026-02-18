"""Tier 3: Tests for IATA API functions against real LetsMesh API.

All tests use script_version="test" to signal test traffic per LetsMesh convention.
"""

from __future__ import annotations

import pytest

from installer.config import lookup_iata_code, search_iata_api

pytestmark = pytest.mark.network


class TestSearchIataApi:
    def test_seattle_returns_results(self) -> None:
        results = search_iata_api("Seattle", script_version="test")
        assert len(results) > 0
        codes = [code for code, name in results]
        assert "SEA" in codes

    def test_nonsense_query_empty(self) -> None:
        results = search_iata_api("XXXNOTREAL999", script_version="test")
        assert results == []

    def test_returns_tuples(self) -> None:
        results = search_iata_api("Los Angeles", script_version="test")
        assert len(results) > 0
        for item in results:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], str)


class TestLookupIataCode:
    def test_sea_returns_name(self) -> None:
        name = lookup_iata_code("SEA", script_version="test")
        assert name is not None
        assert isinstance(name, str)
        assert len(name) > 0

    def test_lax_returns_name(self) -> None:
        name = lookup_iata_code("LAX", script_version="test")
        assert name is not None
        assert len(name) > 0

    def test_invalid_code_returns_none(self) -> None:
        name = lookup_iata_code("ZZZ", script_version="test")
        # ZZZ may or may not exist — but truly invalid codes should return None
        # Use a more obviously invalid code
        name2 = lookup_iata_code("999", script_version="test")
        assert name2 is None
