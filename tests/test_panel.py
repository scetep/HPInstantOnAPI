"""Network map panel and topology websocket."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.instant_on.const import DOMAIN

from .test_init import WIRED_MAC, _setup


async def test_setup_without_frontend(hass: HomeAssistant, fake_api, config_entry) -> None:
    """Entities still work when the frontend (and so the map) is unavailable."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.instant_on.panel.async_setup_component", AsyncMock(return_value=False)
    ):
        await _setup(hass, config_entry)
    assert "instant-on" not in hass.data.get(DATA_PANELS, {})
    assert hass.states.get("sensor.test_site_clients_online").state == "46"


async def test_panel_registered_and_removed(hass: HomeAssistant, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    panel = hass.data[DATA_PANELS]["instant-on"]
    assert panel.require_admin
    assert panel.config["_panel_custom"]["module_url"].startswith("/instant_on_static/instant-on-panel.js")

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "instant-on" not in hass.data[DATA_PANELS]


async def test_panel_js_is_served(hass: HomeAssistant, hass_client, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    client = await hass_client()
    resp = await client.get("/instant_on_static/instant-on-panel.js")
    assert resp.status == 200
    assert "instant-on-panel" in await resp.text()


async def test_topology_subscription(
    hass: HomeAssistant, hass_ws_client, fake_api, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, WIRED_MAC)},
        name="Garage door",
    )
    await _setup(hass, config_entry)

    # Put the first switch in a room.
    area = ar.async_get(hass).async_create("Basement")
    dev_reg = dr.async_get(hass)
    switch = next(
        d for d in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id) if d.name == "Switch 2"
    )
    dev_reg.async_update_device(switch.id, area_id=area.id)

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "instant_on/topology/subscribe"})
    assert (await ws.receive_json())["success"]
    msg = (await ws.receive_json())["event"]

    assert msg["updated"]
    site = msg["sites"][0]
    assert site["name"] == "Test Site"
    assert site["health"] == "warning"
    assert len(site["devices"]) == 8
    assert len(site["clients"]) == 58

    by_id = {d["id"]: d for d in site["devices"]}
    for dev in site["devices"]:
        if dev["uplink_id"] in by_id:
            # The uplink port is the parent's port that leads to this device (not the child's own port).
            parent_port = next(p for p in by_id[dev["uplink_id"]]["ports"] if p["number"] == dev["uplink_port"])
            assert parent_port["device_id"] == dev["id"]
            assert not parent_port["uplink"]

    dev2 = next(d for d in site["devices"] if d["name"] == "Switch 2")
    assert dev2["area"] == "Basement"
    assert dev2["ha_device_id"] == switch.id
    assert len(dev2["ports"]) == 10
    assert dev2["uplink_id"] and dev2["uplink_port"] is not None

    client = next(c for c in site["clients"] if c["mac"] == WIRED_MAC)
    assert client["online"] and not client["wireless"]
    assert client["device_id"] == dev2["id"]
    assert client["port"] == 5
    assert client["ha_device_name"] == "Garage door"
    assert client["tracker_entity_id"]

    # Pushed again after the next refresh.
    freezer.tick(timedelta(seconds=70))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await ws.receive_json())["event"]["sites"][0]["id"] == site["id"]

    # And after the entry reloads (e.g. options changed).
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert (await ws.receive_json())["event"]["sites"]


async def test_topology_requires_admin(hass: HomeAssistant, hass_ws_client, hass_admin_user, fake_api, config_entry) -> None:
    await _setup(hass, config_entry)
    hass_admin_user.groups = []
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "instant_on/topology/subscribe"})
    result = await ws.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "unauthorized"


async def test_remove_stale_device(hass: HomeAssistant, hass_ws_client, fake_api, config_entry) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "config", {})
    await _setup(hass, config_entry)
    dev_reg = dr.async_get(hass)
    current = next(
        d for d in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id) if d.name == "Switch 2"
    )
    stale = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, "02:00:00:00:00:01")}, name="Old AP"
    )
    ws = await hass_ws_client(hass)
    for i, device in enumerate((current, stale), start=1):
        await ws.send_json({"id": i, "type": "config/device_registry/remove", "device_id": device.id})
        result = await ws.receive_json()
        if device is stale:
            assert result["success"], result
        else:
            assert not result["success"]
            assert result["error"]["code"] != "invalid_format", result
