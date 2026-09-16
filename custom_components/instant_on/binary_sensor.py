"""Binary sensors for Instant On."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import InstantOnConfigEntry
from .coordinator import InstantOnCoordinator
from .entity import DeviceEntity, PortEntity, SiteEntity, ports_of

SPEED_LABEL = {
    "mbps10": "10 Mbps", "mbps100": "100 Mbps", "mbps1000": "1 Gbps", "mbps2500": "2.5 Gbps",
    "mbps5000": "5 Gbps", "mbps10000": "10 Gbps",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InstantOnConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    fast = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new: list[BinarySensorEntity] = []
        for site_id, data in (fast.data or {}).items():
            if site_id not in known:
                known.add(site_id)
                new.append(SiteProblemSensor(fast, fast, site_id, "problem"))
            for dev_id, dev in data.devices.items():
                if dev_id not in known:
                    known.add(dev_id)
                    new += [
                        DeviceOnlineSensor(fast, site_id, dev_id, "online"),
                        DeviceUpToDateSensor(fast, site_id, dev_id, "firmware_outdated"),
                    ]
                for port in ports_of(dev):
                    key = f"{dev_id}_port_{port['portNumber']}"
                    if key not in known:
                        known.add(key)
                        new.append(PortLinkSensor(fast, site_id, dev_id, port["portNumber"], "link"))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(fast.async_add_listener(add_new))


class SiteProblemSensor(SiteEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        health = self.site_data.site.get("health")
        return None if health is None else health != "good"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        site = self.site_data.site
        return {
            "health": site.get("health"),
            "reason": site.get("healthReason"),
            "active_alerts": site.get("activeAlertsCount"),
        }


class DeviceOnlineSensor(DeviceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "device_online"

    @property
    def is_on(self) -> bool:
        return self.device.get("status") == "up"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.device
        return {
            "status": d.get("status"),
            "operational_state": d.get("operationalState"),
            "health_conditions": d.get("healthConditions") or [],
            "uplink_device": d.get("uplinkDeviceName"),
            "seconds_since_last_contact": d.get("numberOfSecondsSinceLastCommunication"),
            "power_source": d.get("inputPowerSource"),
            "underpowered": d.get("isUnderpowered"),
        }


class DeviceUpToDateSensor(DeviceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_firmware_outdated"

    @property
    def is_on(self) -> bool | None:
        up_to_date = self.device.get("isUpToDate")
        return None if up_to_date is None else not up_to_date

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"firmware": self.device.get("currentFirmwareVersion")}


class PortLinkSensor(PortEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: InstantOnCoordinator, site_id: str, device_id: str, port_number: int, key: str) -> None:
        super().__init__(coordinator, site_id, device_id, port_number, key)

    @property
    def is_on(self) -> bool:
        return bool(self.port.get("isLinkUp"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.port
        data = self.site_data
        clients = data.port_clients(self.device_id, self.port_number) if data else []
        attrs: dict[str, Any] = {
            "speed": SPEED_LABEL.get(p.get("speed"), p.get("speed")) if p.get("isLinkUp") else None,
            "duplex": p.get("duplex"),
            "uplink": bool(p.get("isUplink")),
            "disabled": bool(p.get("userDeactivated")),
            "connected_device": p.get("directlyConnectedDeviceName"),
            "clients": [
                {"name": c.get("name"), "mac": c.get("macAddress"), "online": c.get("status") == "up"}
                for c in clients
            ],
            "poe_status": p.get("poePseStatus") if p.get("isPoeSupported") else None,
            "poe_watts": round((p.get("powerProvidedInMilliwatts") or 0) / 1000, 1)
            if p.get("isPoeSupported") else None,
        }
        problems = [
            name
            for name, flag in (
                ("loop", "isLoopDetected"),
                ("link_flapping", "isLinkFlappingDetected"),
                ("bpdu_guard", "isBpduGuardDetected"),
                ("bpdu_storm", "isBpduStormDetected"),
            )
            if p.get(flag)
        ]
        if problems:
            attrs["problems"] = problems
        return attrs
