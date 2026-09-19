"""Network map panel: topology websocket API and sidebar panel registration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component

from .const import DOMAIN, SIGNAL_ENTRIES_CHANGED
from .coordinator import SiteData, normalize_mac

PANEL_URL_PATH = "instant-on"
PANEL_COMPONENT = "instant-on-panel"
STATIC_URL = "/instant_on_static"
FRONTEND_DIR = Path(__file__).parent / "frontend"
DATA_PANEL = f"{DOMAIN}_panel"

_LOGGER = logging.getLogger(__name__)


async def async_setup_panel_support(hass: HomeAssistant) -> None:
    """Static files and websocket commands (once per HA run)."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), cache_headers=False)]
    )
    websocket_api.async_register_command(hass, ws_subscribe_topology)


async def async_register_panel(hass: HomeAssistant, version: str) -> bool:
    """Add the map to the sidebar; the integration works fine without it."""
    if hass.data.get(DATA_PANEL):
        return True
    if "panel_custom" not in hass.config.components and not await async_setup_component(
        hass, "panel_custom", {}
    ):
        _LOGGER.warning("Frontend is unavailable, so the Instant On network map is not shown")
        return False
    hass.data[DATA_PANEL] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title="Instant On",
        sidebar_icon="mdi:lan",
        module_url=f"{STATIC_URL}/instant-on-panel.js?v={version}",
        require_admin=True,
    )
    return True


@callback
def async_unregister_panel(hass: HomeAssistant) -> None:
    if hass.data.pop(DATA_PANEL, None):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


def _active_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Loaded entries, plus ones finishing setup whose data is already available."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        or (entry.state is ConfigEntryState.SETUP_IN_PROGRESS and hasattr(entry, "runtime_data"))
    ]


def _speed(value: str | None) -> int | None:
    if not value or not value.startswith("mbps"):
        return None
    try:
        return int(value[4:])
    except ValueError:
        return None


def _ha_links(hass: HomeAssistant, entry_id: str, mac: str) -> dict[str, Any]:
    """HA devices (from any integration) and our tracker entity for a MAC."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    devices = dev_reg.async_get_devices(connections={(dr.CONNECTION_NETWORK_MAC, mac)})
    others = [d for d in devices if entry_id not in d.config_entries]
    ours = [d for d in devices if entry_id in d.config_entries]
    tracker = ent_reg.async_get_entity_id("device_tracker", DOMAIN, mac)
    best = (others or ours or [None])[0]
    return {
        "ha_device_id": best.id if best else None,
        "ha_device_name": (best.name_by_user or best.name) if best else None,
        "area": _area_name(hass, best.area_id if best else None),
        "tracker_entity_id": tracker,
    }


def _area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def build_site_topology(hass: HomeAssistant, entry_id: str, data: SiteData) -> dict[str, Any]:
    site = data.site
    dev_reg = dr.async_get(hass)
    our_devices = {
        ident: device
        for device in dr.async_entries_for_config_entry(dev_reg, entry_id)
        for domain, ident in device.identifiers
        if domain == DOMAIN
    }
    devices_out = []
    parent_port: dict[str, dict[str, Any]] = {}
    for dev in data.devices.values():
        for p in dev.get("ethernetPorts") or []:
            child = data.devices.get(p.get("directlyConnectedDeviceId") or "")
            # Only ports leading downstream; an AP's own uplink port also names its switch.
            if child and child.get("uplinkDeviceDeviceId") == dev["id"]:
                parent_port[child["id"]] = {"port": p.get("portNumber"), "speed": _speed(p.get("speed"))}

    for dev in data.devices.values():
        ha_dev = our_devices.get(dev["id"])
        uplink = parent_port.get(dev["id"], {})
        devices_out.append(
            {
                "id": dev["id"],
                "name": dev.get("name") or dev.get("defaultName"),
                "type": dev.get("deviceType"),
                "model": dev.get("model"),
                "status": dev.get("status"),
                "health": dev.get("health"),
                "ip": dev.get("ipAddress"),
                "mac": normalize_mac(dev.get("macAddress") or dev["id"]),
                "firmware": dev.get("currentFirmwareVersion"),
                "up_to_date": dev.get("isUpToDate"),
                "uptime": dev.get("uptimeInSeconds"),
                "uplink_id": dev.get("uplinkDeviceDeviceId"),
                "uplink_port": uplink.get("port"),
                "uplink_speed": uplink.get("speed"),
                "power_source": dev.get("inputPowerSource"),
                "poe_budget": dev.get("poePseNominalPowerInWatts"),
                "poe_used": dev.get("poePseConsumedPowerInWatts"),
                "gateway": dev.get("defaultGateway"),
                "ha_device_id": ha_dev.id if ha_dev else None,
                "ha_name": (ha_dev.name_by_user or ha_dev.name) if ha_dev else None,
                "area": _area_name(hass, ha_dev.area_id) if ha_dev else None,
                "radios": [
                    {
                        "band": r.get("band"),
                        "channel": r.get("channel"),
                        "width": r.get("channelWidth"),
                        "utilization": r.get("utilizationPercent"),
                        "clients": r.get("wirelessClientsCount"),
                        "tx_power": r.get("txPowerEirpInDbm"),
                    }
                    for r in dev.get("radios") or []
                ],
                "ports": [
                    {
                        "number": p.get("portNumber"),
                        "label": p.get("faceplatePortNumber") or p.get("portNumber"),
                        "name": p.get("name"),
                        "up": bool(p.get("isLinkUp")),
                        "speed": _speed(p.get("speed")) if p.get("isLinkUp") else None,
                        "max_speed": _speed(p.get("maxSpeed")),
                        "uplink": bool(p.get("isUplink")),
                        "disabled": bool(p.get("userDeactivated")),
                        "sfp": bool(p.get("isSfp")),
                        "poe": bool(p.get("isPoeSupported")),
                        "poe_watts": round((p.get("powerProvidedInMilliwatts") or 0) / 1000, 1),
                        "device_id": p.get("directlyConnectedDeviceId"),
                        "down_bps": p.get("downstreamThroughputInBitsPerSecond"),
                        "up_bps": p.get("upstreamThroughputInBitsPerSecond"),
                        "problems": [
                            name
                            for name, flag in (
                                ("loop", "isLoopDetected"),
                                ("link flapping", "isLinkFlappingDetected"),
                                ("BPDU guard", "isBpduGuardDetected"),
                                ("BPDU storm", "isBpduStormDetected"),
                            )
                            if p.get(flag)
                        ],
                    }
                    for p in dev.get("ethernetPorts") or []
                    if p.get("portNumber") is not None
                ],
            }
        )

    clients_out = []
    for mac, c in data.clients.items():
        ports = c.get("connectedToPorts") or []
        port = ports[0] if ports else {}
        traffic = c.get("dataTraffic") or {}
        wireless = bool(c.get("wirelessNetworkId"))
        clients_out.append(
            {
                "mac": mac,
                "name": c.get("name") or c.get("hostName") or mac,
                "ip": c.get("ipAddress"),
                "online": c.get("status") == "up",
                "wireless": wireless,
                "device_id": port.get("deviceId") or c.get("deviceId"),
                "port": None if wireless else port.get("portNumber"),
                "port_speed": _speed(port.get("portSpeed")),
                "band": c.get("wirelessBand"),
                "signal": c.get("signalInDbm"),
                "snr": c.get("snrInDb"),
                "quality": c.get("signalQuality"),
                "network": c.get("wirelessNetworkName") or c.get("wiredNetworkName")
                or next(
                    (n.get("networkName") for n in port.get("accessedWiredNetworks") or [] if n.get("networkName")),
                    None,
                ),
                "vlan": c.get("vlanId"),
                "health": c.get("health"),
                "health_pct": c.get("healthInPercent"),
                "os": c.get("detectedOs"),
                "since": c.get("lastStateChange"),
                "connected_for": c.get("connectionDurationInSeconds"),
                "down_24h": traffic.get("downstreamDataTransferredInBytesInLast24Hours"),
                "up_24h": traffic.get("upstreamDataTransferredInBytesInLast24Hours"),
                "watchlisted": bool(c.get("isWatchlisted")),
                "blocked": bool(c.get("isBlocked")),
                **_ha_links(hass, entry_id, mac),
            }
        )

    alert = site.get("latestActiveAlert") or {}
    return {
        "id": data.id,
        "name": data.name,
        "health": site.get("health"),
        "health_reason": site.get("healthReason"),
        "health_score": (site.get("currentHealthScore") or {}).get("score"),
        "active_alerts": site.get("activeAlertsCount", 0),
        "latest_alert": {
            "type": alert.get("type"),
            "severity": alert.get("severity"),
            "subject": (alert.get("alertTypeProperties") or {}).get("clientName"),
            "raised": alert.get("raisedTime"),
        }
        if alert
        else None,
        "gateways": sorted({d["gateway"] for d in devices_out if d.get("gateway")}),
        "devices": devices_out,
        "clients": clients_out,
    }


@callback
def build_topology(hass: HomeAssistant) -> dict[str, Any]:
    sites = []
    updated = None
    for entry in _active_entries(hass):
        coordinator = entry.runtime_data.coordinator
        for data in (coordinator.data or {}).values():
            sites.append(build_site_topology(hass, entry.entry_id, data))
        if coordinator.last_refresh and (updated is None or coordinator.last_refresh > updated):
            updated = coordinator.last_refresh
    return {"sites": sites, "updated": updated.isoformat() if updated else None}


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "instant_on/topology/subscribe"})
@callback
def ws_subscribe_topology(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the topology now and after every coordinator refresh."""
    msg_id = msg["id"]

    @callback
    def send() -> None:
        connection.send_message(websocket_api.event_message(msg_id, build_topology(hass)))

    listeners: list = []

    @callback
    def bind() -> None:
        # Entries reload when options change; follow their new coordinators.
        while listeners:
            listeners.pop()()
        listeners.extend(
            entry.runtime_data.coordinator.async_add_listener(send)
            for entry in _active_entries(hass)
        )

    @callback
    def entries_changed(loaded: bool) -> None:
        bind()
        if loaded:
            send()

    bind()
    unsub_signal = async_dispatcher_connect(hass, SIGNAL_ENTRIES_CHANGED, entries_changed)

    @callback
    def unsubscribe() -> None:
        unsub_signal()
        while listeners:
            listeners.pop()()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    send()
