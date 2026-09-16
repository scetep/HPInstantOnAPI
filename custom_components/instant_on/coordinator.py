"""Data coordinators for Instant On."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    InstantOnAuthError,
    InstantOnClient,
    InstantOnConnectionError,
    InstantOnError,
    InstantOnRateLimitedError,
    elements,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SITES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_ENDPOINTS,
    EVENT_INSTANT_ON,
    EVENT_INSTANT_ON_ALERT,
    EVENTS_INTERVAL,
    FAST_ENDPOINTS,
    JITTER,
    MAX_BACKOFF,
    MIN_SCAN_INTERVAL,
    SLOW_ENDPOINTS,
    SLOW_UPDATE_INTERVAL,
)

if TYPE_CHECKING:
    from . import InstantOnConfigEntry

_LOGGER = logging.getLogger(__name__)


def normalize_mac(mac: str | None) -> str:
    return (mac or "").lower().replace("-", ":")


@dataclass
class SiteData:
    """Fast-changing state of one site."""

    site: dict[str, Any]
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.site["id"]

    @property
    def name(self) -> str:
        return self.site.get("name") or self.site["id"]

    def port_clients(self, device_id: str, port_number: int) -> list[dict[str, Any]]:
        """Clients seen on a given switch/AP ethernet port."""
        return [
            c
            for c in self.clients.values()
            if any(
                p.get("deviceId") == device_id and p.get("portNumber") == port_number
                for p in c.get("connectedToPorts") or []
            )
        ]


class BackoffCoordinator[T](DataUpdateCoordinator[T]):
    """Polls at a jittered base interval and backs off after throttling or server errors."""

    config_entry: InstantOnConfigEntry

    def __init__(self, hass: HomeAssistant, entry: InstantOnConfigEntry, name: str, interval: timedelta) -> None:
        self.base_interval = interval * (1 + random.uniform(0, JITTER))
        super().__init__(hass, _LOGGER, config_entry=entry, name=name, update_interval=self.base_interval)
        self.failures = 0

    @property
    def backing_off(self) -> bool:
        return self.failures > 0

    def _succeeded(self) -> None:
        if self.failures:
            _LOGGER.info("%s: Instant On is responding again, polling every %s", self.name, self.base_interval)
        self.failures = 0
        self.update_interval = self.base_interval

    def _failed(self, err: InstantOnError) -> Exception:
        if isinstance(err, InstantOnAuthError):
            return ConfigEntryAuthFailed(str(err))
        if not isinstance(err, InstantOnConnectionError):
            return UpdateFailed(str(err))
        self.failures += 1
        delay = min(self.base_interval * 2**self.failures, MAX_BACKOFF)
        retry_after = getattr(err, "retry_after", None)
        if retry_after:
            delay = max(delay, timedelta(seconds=retry_after))
        self.update_interval = delay
        kind = "Rate limited by Instant On" if isinstance(err, InstantOnRateLimitedError) else "Instant On error"
        return UpdateFailed(f"{kind}: {err}; retrying in {int(delay.total_seconds())} s")


class InstantOnCoordinator(BackoffCoordinator[dict[str, SiteData]]):
    """Sites, devices, ports and clients (every scan interval)."""

    def __init__(
        self, hass: HomeAssistant, entry: InstantOnConfigEntry, client: InstantOnClient
    ) -> None:
        seconds = max(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL), MIN_SCAN_INTERVAL)
        super().__init__(hass, entry, f"{DOMAIN} {entry.title}", timedelta(seconds=seconds))
        self.client = client
        self._last_event_time: dict[str, int] = {}
        self._last_alert_id: dict[str, str | None] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._events_fetched: dict[str, Any] = {}
        self.last_refresh = None

    @property
    def selected_sites(self) -> list[str] | None:
        return self.config_entry.options.get(CONF_SITES) or None

    async def _async_update_data(self) -> dict[str, SiteData]:
        try:
            sites = await self.client.sites()
            wanted = self.selected_sites
            result: dict[str, SiteData] = {}
            for site in sites:
                if wanted and site["id"] not in wanted:
                    continue
                now = dt_util.utcnow()
                last_events = self._events_fetched.get(site["id"])
                fetch_events = last_events is None or now - last_events >= EVENTS_INTERVAL
                eps = await self.client.site_endpoints(
                    site["id"], FAST_ENDPOINTS + (EVENT_ENDPOINTS if fetch_events else [])
                )
                data = SiteData(site=site)
                data.devices = {d["id"]: d for d in elements(eps["inventory"])}
                data.clients = {
                    normalize_mac(c.get("macAddress") or c.get("id")): c
                    for c in elements(eps["clientSummary"])
                }
                if fetch_events:
                    self._events[site["id"]] = elements(eps["events"])
                    self._events_fetched[site["id"]] = now
                data.events = self._events.get(site["id"], [])
                result[site["id"]] = data
        except InstantOnError as err:
            raise self._failed(err) from err

        self._succeeded()
        for data in result.values():
            self._fire_events(data)
        self.last_refresh = dt_util.utcnow()
        return result

    def _fire_events(self, data: SiteData) -> None:
        """Forward new portal events and newly raised alerts to the HA event bus."""
        newest = max((e.get("occurrenceTime") or 0 for e in data.events), default=0)
        last = self._last_event_time.get(data.id)
        if last is not None:
            for ev in sorted(data.events, key=lambda e: e.get("occurrenceTime") or 0):
                if (ev.get("occurrenceTime") or 0) <= last:
                    continue
                self.hass.bus.async_fire(
                    EVENT_INSTANT_ON,
                    {
                        "site_id": data.id,
                        "site_name": data.name,
                        "event": ev.get("event"),
                        "state": ev.get("state"),
                        "category": ev.get("category"),
                        "source_id": (ev.get("source") or {}).get("id"),
                        "source_name": (ev.get("source") or {}).get("name"),
                        "occurred": ev.get("occurrenceTime"),
                        "attributes": {
                            a.get("key"): a.get("valueStr") or a.get("valueEnum")
                            for a in ev.get("attributes") or []
                        },
                    },
                )
        self._last_event_time[data.id] = max(newest, last or 0)

        alert = data.site.get("latestActiveAlert") or {}
        alert_id = alert.get("id")
        if data.id in self._last_alert_id and alert_id and alert_id != self._last_alert_id[data.id]:
            props = alert.get("alertTypeProperties") or {}
            self.hass.bus.async_fire(
                EVENT_INSTANT_ON_ALERT,
                {
                    "site_id": data.id,
                    "site_name": data.name,
                    "alert_id": alert_id,
                    "type": alert.get("type"),
                    "severity": alert.get("severity"),
                    "raised": alert.get("raisedTime"),
                    "client_name": props.get("clientName"),
                    "client_id": props.get("clientId"),
                    "device_names": props.get("deviceNames"),
                },
            )
        self._last_alert_id[data.id] = alert_id

    def find_mac(self, mac: str) -> dict[str, Any] | None:
        """Locate a MAC among clients and infrastructure devices."""
        mac = normalize_mac(mac)
        for data in (self.data or {}).values():
            if client := data.clients.get(mac):
                ports = client.get("connectedToPorts") or []
                return {
                    "found": True,
                    "kind": "client",
                    "site_id": data.id,
                    "site_name": data.name,
                    "mac": mac,
                    "name": client.get("name"),
                    "ip_address": client.get("ipAddress"),
                    "online": client.get("status") == "up",
                    "connection": client.get("clientType"),
                    "network": client.get("wirelessNetworkName") or client.get("wiredNetworkName"),
                    "vlan": client.get("vlanId"),
                    "connected_device": client.get("deviceName"),
                    "ports": [
                        {"device": p.get("deviceName"), "port": p.get("portNumber"), "speed": p.get("portSpeed")}
                        for p in ports
                    ],
                    "band": client.get("wirelessBand"),
                    "signal_dbm": client.get("signalInDbm"),
                    "health": client.get("health"),
                    "last_state_change": client.get("lastStateChange"),
                    "watchlisted": client.get("isWatchlisted"),
                }
            for dev in data.devices.values():
                if normalize_mac(dev.get("macAddress") or dev["id"]) == mac:
                    return {
                        "found": True,
                        "kind": "device",
                        "site_id": data.id,
                        "site_name": data.name,
                        "mac": mac,
                        "name": dev.get("name"),
                        "model": dev.get("model"),
                        "ip_address": dev.get("ipAddress"),
                        "online": dev.get("status") == "up",
                        "uplink_device": dev.get("uplinkDeviceName"),
                    }
        return None


class InstantOnSlowCoordinator(BackoffCoordinator[dict[str, dict[str, Any]]]):
    """Site-level configuration and statistics that change slowly."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: InstantOnConfigEntry,
        client: InstantOnClient,
        fast: InstantOnCoordinator,
    ) -> None:
        super().__init__(hass, entry, f"{DOMAIN} {entry.title} (slow)", SLOW_UPDATE_INTERVAL)
        self.client = client
        self.fast = fast

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if self.fast.backing_off and self.data is not None:
            # Don't add load while Instant On is throttling us; keep the last values.
            return self.data
        try:
            result = {
                site_id: await self.client.site_endpoints(site_id, SLOW_ENDPOINTS)
                for site_id in (self.fast.data or {})
            }
        except InstantOnError as err:
            raise self._failed(err) from err
        self._succeeded()
        return result
