"""Setup, entities, services, events and diagnostics."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.instant_on.api import (
    InstantOnAuthError,
    InstantOnConnectionError,
)
from custom_components.instant_on.const import (
    CONF_REFRESH_TOKEN,
    CONF_TRACK_CLIENTS,
    DOMAIN,
    EVENT_INSTANT_ON,
    EVENT_INSTANT_ON_ALERT,
    TRACK_WATCHLIST,
)

WIRED_MAC = "02:f5:89:17:af:eb"  # online, on Switch 2 port 5
WATCHED_MAC = "02:6f:44:d1:40:47"  # watchlisted, offline


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, expect: bool = True) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is expect
    await hass.async_block_till_done()


async def test_setup_creates_entities(hass: HomeAssistant, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.test_site_health").state == "warning"
    assert hass.states.get("sensor.test_site_clients_online").state == "46"
    assert hass.states.get("sensor.test_site_active_alerts").state == "1"
    assert hass.states.get("binary_sensor.test_site_problem").state == STATE_ON
    assert hass.states.get("sensor.test_site_data_transferred_24h") is not None
    assert hass.states.get("update.test_site_firmware").state == STATE_OFF

    assert hass.states.get("binary_sensor.switch_2_online").state == STATE_ON
    assert hass.states.get("sensor.switch_2_poe_budget").state == "124"
    assert hass.states.get("sensor.ap_1_2_4_ghz_utilization") is not None

    port5 = hass.states.get("binary_sensor.switch_2_port_5")
    assert port5.state == STATE_ON
    assert WIRED_MAC in [c["mac"] for c in port5.attributes["clients"]]
    # Port statistics sensors are opt-in.
    assert hass.states.get("sensor.switch_2_port_5_speed") is None

    ent_reg = er.async_get(hass)
    link_sensors = [
        e for e in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
        if e.domain == "binary_sensor" and "_port" in e.unique_id
    ]
    assert len(link_sensors) == 50

    trackers = [
        e for e in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
        if e.domain == "device_tracker"
    ]
    assert len(trackers) == 58
    # No HA device knows these MACs yet, so trackers start disabled.
    assert all(e.disabled_by is not None for e in trackers)

    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    assert len(devices) == 9  # site + 8 network devices


async def test_tracker_links_to_existing_device(hass: HomeAssistant, fake_api, config_entry) -> None:
    """A MAC already known to HA (e.g. from ESPHome) gets an enabled tracker sharing that MAC."""
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, WIRED_MAC)},
        name="Garage door",
    )
    await _setup(hass, config_entry)

    entity_id = er.async_get(hass).async_get_entity_id("device_tracker", DOMAIN, WIRED_MAC)
    entry = er.async_get(hass).async_get(entity_id)
    assert entry.disabled_by is None
    # HA keeps one device per integration; both carry the same MAC connection.
    tracker_device = dr.async_get(hass).async_get(entry.device_id)
    assert tracker_device.id != device.id
    assert (dr.CONNECTION_NETWORK_MAC, WIRED_MAC) in tracker_device.connections
    assert len(dr.async_get(hass).async_get_devices(
        connections={(dr.CONNECTION_NETWORK_MAC, WIRED_MAC)}
    )) == 2
    state = hass.states.get(entity_id)
    assert state.state == "home"
    assert state.attributes["switch_port"] == "Switch 2 port 5"


async def test_watchlist_mode(hass: HomeAssistant, fake_api, config_entry) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_TRACK_CLIENTS: TRACK_WATCHLIST}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    trackers = [
        e for e in er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
        if e.domain == "device_tracker"
    ]
    assert [e.unique_id for e in trackers] == [WATCHED_MAC]


async def test_lookup_mac_service(hass: HomeAssistant, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    result = await hass.services.async_call(
        DOMAIN, "lookup_mac", {"mac": WIRED_MAC.upper()}, blocking=True, return_response=True
    )
    assert result["found"] is True
    assert result["kind"] == "client"
    assert result["online"] is True
    assert result["ports"][0] == {"device": "Switch 2", "port": 5, "speed": result["ports"][0]["speed"]}

    result = await hass.services.async_call(
        DOMAIN, "lookup_mac", {"mac": "00:00:00:00:00:00"}, blocking=True, return_response=True
    )
    assert result == {"found": False, "mac": "00:00:00:00:00:00"}


async def test_new_events_and_alerts_fire(
    hass: HomeAssistant, fake_api, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    events = async_capture_events(hass, EVENT_INSTANT_ON)
    alerts = async_capture_events(hass, EVENT_INSTANT_ON_ALERT)

    newest = fake_api.data["events"]["elements"][0]
    fake_api.data["events"]["elements"].insert(
        0, {**newest, "id": "new", "event": "clientDisconnected", "occurrenceTime": newest["occurrenceTime"] + 5}
    )
    site = fake_api.data["sites"]["elements"][0]
    site["latestActiveAlert"] = {**site["latestActiveAlert"], "id": "alert-2", "severity": "major"}

    # Alerts come with every poll; the event log is only fetched every 5 minutes.
    freezer.tick(timedelta(seconds=70))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [a.data["severity"] for a in alerts] == ["major"]
    assert events == []

    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [e.data["event"] for e in events] == ["clientDisconnected"]
    assert events[0].data["site_name"] == "Test Site"


async def test_new_client_adds_tracker(
    hass: HomeAssistant, fake_api, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    clients = fake_api.data["clientSummary"]["elements"]
    clients.append({**clients[0], "id": "02:00:00:00:00:99", "macAddress": "02:00:00:00:00:99"})
    freezer.tick(timedelta(seconds=70))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert er.async_get(hass).async_get_entity_id("device_tracker", DOMAIN, "02:00:00:00:00:99")


async def test_refresh_token_rotation_is_persisted(hass: HomeAssistant, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    auth = config_entry.runtime_data.client.auth
    auth._store_tokens({"access_token": "a2", "refresh_token": "rotated", "expires_in": 1800})
    await hass.async_block_till_done()
    assert config_entry.data[CONF_REFRESH_TOKEN] == "rotated"
    # Token rotation must not reload the entry.
    assert config_entry.state is ConfigEntryState.LOADED


async def test_expired_session_starts_reauth(hass: HomeAssistant, config_entry) -> None:
    with patch(
        "custom_components.instant_on.api.InstantOnAuth.access_token",
        AsyncMock(side_effect=InstantOnAuthError("invalid_grant")),
    ):
        await _setup(hass, config_entry, expect=False)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_cloud_down_retries(hass: HomeAssistant, config_entry) -> None:
    with patch(
        "custom_components.instant_on.api.InstantOnAuth.access_token",
        AsyncMock(side_effect=InstantOnConnectionError("timeout")),
    ):
        await _setup(hass, config_entry, expect=False)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_and_remove_revokes(hass: HomeAssistant, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    with patch(
        "custom_components.instant_on.api.InstantOnAuth.revoke", autospec=True
    ) as revoke:
        await hass.config_entries.async_remove(config_entry.entry_id)
    assert revoke.call_count == 1


async def test_diagnostics_are_redacted(hass: HomeAssistant, hass_client, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    text = str(diag)
    assert "stored-refresh" not in text
    assert "user@example.com" not in text
    assert WIRED_MAC not in text
    site = next(iter(diag["sites"].values()))
    assert site["networksSummary"]["elements"][0]["preSharedKey"] == "**REDACTED**"
    assert site["inventory"][0]["name"] == "**REDACTED**"
