"""Shared fixtures: a fake Instant On account backed by anonymized JSON."""

from __future__ import annotations

import copy
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.instant_on.api import Tokens
from custom_components.instant_on.const import (
    CONF_PORT_SENSORS,
    CONF_REFRESH_TOKEN,
    CONF_TRACK_CLIENTS,
    DOMAIN,
    TRACK_ALL,
)

FIXTURES = Path(__file__).parent / "fixtures"
USERNAME = "user@example.com"


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


class FakeApi:
    """Serves fixture data for GET /api/<path>; tests can mutate `data`."""

    def __init__(self) -> None:
        self.data = {
            name: load(name)
            for name in (
                "sites", "inventory", "clientSummary", "events", "landingPage",
                "maintenance", "networksSummary", "wiredNetworks",
            )
        }
        self.site_id = self.data["sites"]["elements"][0]["id"]
        self.calls: list[str] = []
        # Errors raised by the next "sites/" calls (simulates throttling/outages).
        self.fail_sites: list[Exception] = []

    async def get(self, path: str) -> Any:
        self.calls.append(path)
        path = path.strip("/")
        if path == "sites" and self.fail_sites:
            raise self.fail_sites.pop(0)
        if path == "sites":
            return copy.deepcopy(self.data["sites"])
        _, _site, endpoint = path.split("/")
        return copy.deepcopy(self.data.get(endpoint))


@pytest.fixture
def fake_api() -> Generator[FakeApi]:
    api = FakeApi()
    with (
        patch("custom_components.instant_on.api.InstantOnClient.get", side_effect=api.get),
        patch(
            "custom_components.instant_on.api.InstantOnAuth.access_token",
            AsyncMock(return_value="access"),
        ),
    ):
        yield api


@pytest.fixture
def mock_login() -> Generator[AsyncMock]:
    tokens = Tokens("access", "refresh-from-login", 9e9)

    async def login(self, username, password, otp=None):
        self._tokens = tokens
        return tokens

    with patch(
        "custom_components.instant_on.api.InstantOnAuth.login", autospec=True, side_effect=login
    ) as mock:
        yield mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Site",
        unique_id=USERNAME,
        data={"username": USERNAME, CONF_REFRESH_TOKEN: "stored-refresh"},
        options={CONF_TRACK_CLIENTS: TRACK_ALL, CONF_PORT_SENSORS: False, "scan_interval": 60},
    )
