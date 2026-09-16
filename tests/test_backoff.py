"""Polling politeness: backoff, Retry-After, event throttling, User-Agent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.instant_on.api import (
    USER_AGENT,
    InstantOnAuth,
    InstantOnClient,
    InstantOnConnectionError,
    InstantOnRateLimitedError,
)
from custom_components.instant_on.const import MAX_BACKOFF

from .test_init import _setup


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_backoff_and_recovery(hass: HomeAssistant, fake_api, config_entry, freezer) -> None:
    await _setup(hass, config_entry)
    fast = config_entry.runtime_data.coordinator
    slow = config_entry.runtime_data.slow
    base = fast.base_interval
    assert timedelta(seconds=60) <= base <= timedelta(seconds=66)

    fake_api.fail_sites = [InstantOnRateLimitedError("429"), InstantOnConnectionError("502")]
    fake_api_calls = len(fake_api.calls)

    await _tick(hass, freezer, base + timedelta(seconds=1))
    assert not fast.last_update_success
    assert fast.failures == 1
    assert fast.update_interval == base * 2
    assert hass.states.get("sensor.test_site_clients_online").state == "unavailable"

    # The slow coordinator keeps its data instead of adding load while we back off.
    slow_calls = len(fake_api.calls)
    await slow.async_refresh()
    assert slow.last_update_success
    assert len(fake_api.calls) == slow_calls

    await _tick(hass, freezer, base * 2 + timedelta(seconds=1))
    assert fast.failures == 2
    assert fast.update_interval == base * 4

    await _tick(hass, freezer, base * 4 + timedelta(seconds=1))
    assert fast.last_update_success
    assert fast.failures == 0
    assert fast.update_interval == base
    assert hass.states.get("sensor.test_site_clients_online").state == "46"
    assert len(fake_api.calls) > fake_api_calls


async def test_backoff_is_capped_and_honors_retry_after(hass: HomeAssistant, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    fast = config_entry.runtime_data.coordinator

    fast.failures = 10
    fast._failed(InstantOnConnectionError("boom"))
    assert fast.update_interval == MAX_BACKOFF

    fast.failures = 0
    fast._failed(InstantOnRateLimitedError("429", retry_after=3600))
    assert fast.update_interval == timedelta(hours=1)


async def test_events_fetched_every_five_minutes(hass: HomeAssistant, fake_api, config_entry, freezer) -> None:
    await _setup(hass, config_entry)
    base = config_entry.runtime_data.coordinator.base_interval

    def event_calls() -> int:
        return sum(1 for c in fake_api.calls if c.endswith("/events"))

    assert event_calls() == 1
    for _ in range(3):
        await _tick(hass, freezer, base + timedelta(seconds=1))
    assert event_calls() == 1
    await _tick(hass, freezer, timedelta(minutes=2))
    await _tick(hass, freezer, base + timedelta(seconds=1))
    assert event_calls() == 2


class _Resp:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content_length = None

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Session:
    """Records headers; serves settings.json, a token refresh and one API call."""

    def __init__(self, api_status: int = 200, api_headers: dict[str, str] | None = None) -> None:
        self.headers: list[dict[str, str]] = []
        self.api_status = api_status
        self.api_headers = api_headers

    def request(self, method: str, url: str, headers: dict[str, str] | None = None, **kw: Any) -> _Resp:
        self.headers.append(headers or {})
        return _Resp(200, {
            "restApiUrl": "https://api.example", "ssoFqdn": "https://sso.example",
            "ssoClientIdAuthZ": "cid", "ssoRedirectUrl": "https://portal.example",
        })

    def post(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> _Resp:
        self.headers.append(headers or {})
        return _Resp(200, {"access_token": "a", "refresh_token": "r2", "expires_in": 1800})

    def get(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> _Resp:
        self.headers.append(headers or {})
        return _Resp(self.api_status, {"elements": []}, self.api_headers)


async def test_user_agent_on_every_request() -> None:
    session = _Session()
    client = InstantOnClient(session, InstantOnAuth(session, "r1"))
    assert await client.sites() == []
    assert len(session.headers) == 3  # settings.json, token refresh, sites
    assert all(h.get("User-Agent") == USER_AGENT for h in session.headers)
    assert USER_AGENT.startswith("ha-instant-on/")


@pytest.mark.parametrize(("headers", "expected"), [({"Retry-After": "120"}, 120.0), ({}, None), ({"Retry-After": "Wed, 21 Oct"}, None)])
async def test_api_429_retry_after(headers: dict[str, str], expected: float | None) -> None:
    session = _Session(api_status=429, api_headers=headers)
    client = InstantOnClient(session, InstantOnAuth(session, "r1"))
    with pytest.raises(InstantOnRateLimitedError) as err:
        await client.sites()
    assert err.value.retry_after == expected
