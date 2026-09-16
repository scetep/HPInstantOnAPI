"""Client device trackers keyed by MAC address.

Home Assistant links a tracker to any existing device (from other integrations)
with the same MAC. Trackers for MACs HA does not know are created disabled.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import InstantOnConfigEntry
from .const import CONF_TRACK_CLIENTS, TRACK_ALL, TRACK_NONE, TRACK_WATCHLIST
from .coordinator import InstantOnCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InstantOnConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    mode = entry.options.get(CONF_TRACK_CLIENTS, TRACK_ALL)
    if mode == TRACK_NONE:
        return
    fast = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = []
        for site_id, data in (fast.data or {}).items():
            for mac, client in data.clients.items():
                if not mac or mac in known:
                    continue
                if mode == TRACK_WATCHLIST and not client.get("isWatchlisted"):
                    continue
                known.add(mac)
                new.append(InstantOnClientTracker(fast, site_id, mac))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(fast.async_add_listener(add_new))


class InstantOnClientTracker(CoordinatorEntity[InstantOnCoordinator], ScannerEntity):
    """A wired or wireless client."""

    def __init__(self, coordinator: InstantOnCoordinator, site_id: str, mac: str) -> None:
        super().__init__(coordinator)
        self.site_id = site_id
        self._attr_mac_address = mac
        self._last: dict[str, Any] = self._client or {}

    @property
    def _client(self) -> dict[str, Any] | None:
        data = (self.coordinator.data or {}).get(self.site_id)
        return data.clients.get(self._attr_mac_address) if data else None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Clients drop out of clientSummary after a while; keep the last known details.
        if client := self._client:
            self._last = client
        super()._handle_coordinator_update()

    @property
    def name(self) -> str:
        return self._last.get("name") or self._attr_mac_address

    @property
    def is_connected(self) -> bool:
        client = self._client
        return bool(client and client.get("status") == "up")

    @property
    def ip_address(self) -> str | None:
        return self._last.get("ipAddress")

    @property
    def hostname(self) -> str | None:
        return self._last.get("hostName") or self._last.get("name")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self._last
        ports = c.get("connectedToPorts") or []
        attrs: dict[str, Any] = {
            "connection": c.get("clientType"),
            "network": c.get("wirelessNetworkName") or c.get("wiredNetworkName"),
            "vlan": c.get("vlanId"),
            "connected_device": c.get("deviceName"),
            "health": c.get("health"),
            "watchlisted": c.get("isWatchlisted"),
            "blocked": c.get("isBlocked"),
        }
        if c.get("wirelessNetworkId"):
            attrs |= {
                "band": c.get("wirelessBand"),
                "signal_dbm": c.get("signalInDbm"),
                "snr_db": c.get("snrInDb"),
                "signal_quality": c.get("signalQuality"),
            }
        if ports:
            attrs["switch_port"] = ", ".join(f"{p.get('deviceName')} port {p.get('portNumber')}" for p in ports)
            attrs["port_speed"] = ports[0].get("portSpeed")
        if c.get("lastStateChange"):
            attrs["last_state_change"] = dt_util.utc_from_timestamp(c["lastStateChange"]).isoformat()
        traffic = c.get("dataTraffic") or {}
        attrs["downstream_bytes_24h"] = traffic.get("downstreamDataTransferredInBytesInLast24Hours")
        attrs["upstream_bytes_24h"] = traffic.get("upstreamDataTransferredInBytesInLast24Hours")
        return attrs
