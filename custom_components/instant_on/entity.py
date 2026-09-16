"""Base entities for Instant On."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN, MANUFACTURER, PORTAL_URL
from .coordinator import InstantOnCoordinator, SiteData, normalize_mac

# HA 2026.8+ links child devices by registry id; older versions by identifier.
_HAS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def ensure_site_device(hass: HomeAssistant, entry_id: str, site: dict[str, Any]) -> str:
    """Register the site (hub) device before its children reference it."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id, **site_device_info(site)
    ).id


def site_device_info(site: dict[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, site["id"])},
        name=site.get("name"),
        manufacturer=MANUFACTURER,
        model="Instant On site",
        configuration_url=PORTAL_URL,
    )


def network_device_info(
    hass: HomeAssistant, entry_id: str, site: dict[str, Any], device: dict[str, Any]
) -> DeviceInfo:
    mac = normalize_mac(device.get("macAddress") or device["id"])
    via: dict[str, Any] = (
        {"via_device_id": ensure_site_device(hass, entry_id, site)}
        if _HAS_VIA_DEVICE_ID
        else {"via_device": (DOMAIN, site["id"])}
    )
    return DeviceInfo(
        **via,
        identifiers={(DOMAIN, device["id"])},
        connections={(CONNECTION_NETWORK_MAC, mac)},
        name=device.get("name") or device.get("defaultName"),
        manufacturer=MANUFACTURER,
        model=device.get("model"),
        model_id=device.get("sku"),
        serial_number=device.get("serialNumber"),
        sw_version=device.get("currentFirmwareVersion") or device.get("deviceSoftwareVersion"),
        configuration_url=PORTAL_URL,
    )


class SiteEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """Entity attached to the site (hub) device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DataUpdateCoordinator, fast: InstantOnCoordinator, site_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self.fast = fast
        self.site_id = site_id
        self._attr_unique_id = f"{site_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = site_device_info(fast.data[site_id].site)

    @property
    def site_data(self) -> SiteData | None:
        return (self.fast.data or {}).get(self.site_id)

    @property
    def available(self) -> bool:
        return super().available and self.site_data is not None


class DeviceEntity(CoordinatorEntity[InstantOnCoordinator]):
    """Entity attached to an access point or switch."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: InstantOnCoordinator, site_id: str, device_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self.site_id = site_id
        self.device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}"
        data = coordinator.data[site_id]
        self._attr_device_info = network_device_info(
            coordinator.hass, coordinator.config_entry.entry_id, data.site, data.devices[device_id]
        )

    @property
    def site_data(self) -> SiteData | None:
        return (self.coordinator.data or {}).get(self.site_id)

    @property
    def device(self) -> dict[str, Any] | None:
        data = self.site_data
        return data.devices.get(self.device_id) if data else None

    @property
    def available(self) -> bool:
        return super().available and self.device is not None


class PortEntity(DeviceEntity):
    """Entity for one ethernet port of an AP or switch."""

    def __init__(
        self, coordinator: InstantOnCoordinator, site_id: str, device_id: str, port_number: int, key: str
    ) -> None:
        super().__init__(coordinator, site_id, device_id, f"port{port_number}_{key}")
        self.port_number = port_number
        self._attr_translation_key = f"port_{key}"
        self._attr_translation_placeholders = {"port": str(port_number)}

    @property
    def port(self) -> dict[str, Any] | None:
        dev = self.device
        if not dev:
            return None
        return next(
            (p for p in dev.get("ethernetPorts") or [] if p.get("portNumber") == self.port_number),
            None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.port is not None


def ports_of(device: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in device.get("ethernetPorts") or [] if p.get("portNumber") is not None]
