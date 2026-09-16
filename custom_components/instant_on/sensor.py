"""Sensors for Instant On."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import InstantOnConfigEntry
from .api import elements
from .const import CONF_PORT_SENSORS
from .coordinator import InstantOnCoordinator, SiteData
from .entity import DeviceEntity, PortEntity, SiteEntity, ports_of

SPEED_MBPS = {
    "mbps10": 10, "mbps100": 100, "mbps1000": 1000, "mbps2500": 2500,
    "mbps5000": 5000, "mbps10000": 10000,
}
BAND_LABEL = {"2.4ghz": "2.4 GHz", "5ghz": "5 GHz", "6ghz": "6 GHz"}


@dataclass(frozen=True, kw_only=True)
class SiteSensorDescription(SensorEntityDescription):
    value_fn: Callable[[SiteData, dict[str, Any]], Any]
    attrs_fn: Callable[[SiteData, dict[str, Any]], dict[str, Any]] | None = None
    slow: bool = False


def _online(data: SiteData) -> list[dict[str, Any]]:
    return [c for c in data.clients.values() if c.get("status") == "up"]


def _latest_alert(data: SiteData, _: dict[str, Any]) -> dict[str, Any]:
    alert = data.site.get("latestActiveAlert") or {}
    props = alert.get("alertTypeProperties") or {}
    counters = data.site.get("activeAlertsCounters") or {}
    return {
        "major": counters.get("activeMajorAlertsCount"),
        "minor": counters.get("activeMinorAlertsCount"),
        "info": counters.get("activeInfoAlertsCount"),
        "latest_type": alert.get("type"),
        "latest_severity": alert.get("severity"),
        "latest_subject": props.get("clientName") or ", ".join(props.get("deviceNames") or []) or None,
        "latest_raised": (
            dt_util.utc_from_timestamp(alert["raisedTime"]).isoformat() if alert.get("raisedTime") else None
        ),
    }


SITE_SENSORS: tuple[SiteSensorDescription, ...] = (
    SiteSensorDescription(
        key="health",
        value_fn=lambda d, _: d.site.get("health"),
        attrs_fn=lambda d, _: {"reason": d.site.get("healthReason"), "trend": d.site.get("healthScoreTrend")},
    ),
    SiteSensorDescription(
        key="health_score",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, _: (d.site.get("currentHealthScore") or {}).get("score"),
    ),
    SiteSensorDescription(
        key="active_alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, _: d.site.get("activeAlertsCount", 0),
        attrs_fn=_latest_alert,
    ),
    SiteSensorDescription(
        key="devices_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, _: sum(1 for x in d.devices.values() if x.get("status") == "up"),
        attrs_fn=lambda d, _: {
            "total": len(d.devices),
            "offline": [x.get("name") for x in d.devices.values() if x.get("status") != "up"],
        },
    ),
    SiteSensorDescription(
        key="clients_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, _: len(_online(d)),
    ),
    SiteSensorDescription(
        key="wireless_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, _: sum(1 for c in _online(d) if c.get("wirelessNetworkId")),
    ),
    SiteSensorDescription(
        key="wired_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, _: sum(1 for c in _online(d) if not c.get("wirelessNetworkId")),
    ),
    SiteSensorDescription(
        key="throughput",
        slow=True,
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _, s: (s.get("landingPage") or {}).get("currentNetworkThroughputInBitsPerSecond"),
    ),
    SiteSensorDescription(
        key="data_24h",
        slow=True,
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _, s: (s.get("landingPage") or {}).get("totalDataTransferredDuringLast24HoursInBytes"),
    ),
    SiteSensorDescription(
        key="networks_active",
        slow=True,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _, s: sum(1 for n in elements(s.get("networksSummary")) if n.get("isEnabled"))
        + sum(1 for n in elements(s.get("wiredNetworks")) if n.get("isEnabled")),
        attrs_fn=lambda _, s: {
            "wireless": [n.get("networkName") for n in elements(s.get("networksSummary")) if n.get("isEnabled")],
            "wired": [
                f"{n.get('wiredNetworkName')} (VLAN {n.get('vlanId')})"
                for n in elements(s.get("wiredNetworks"))
                if n.get("isEnabled")
            ],
        },
    ),
)


@dataclass(frozen=True, kw_only=True)
class DeviceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True


DEVICE_SENSORS: tuple[DeviceSensorDescription, ...] = (
    DeviceSensorDescription(key="health", value_fn=lambda d: d.get("health")),
    DeviceSensorDescription(
        key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("ipAddress"),
    ),
    DeviceSensorDescription(
        key="wireless_clients",
        state_class=SensorStateClass.MEASUREMENT,
        exists_fn=lambda d: bool(d.get("radios")),
        value_fn=lambda d: sum(r.get("wirelessClientsCount") or 0 for r in d.get("radios") or []),
    ),
    DeviceSensorDescription(
        key="wired_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("wiredClientsCount"),
    ),
    DeviceSensorDescription(
        key="poe_consumed",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        exists_fn=lambda d: d.get("poePseNominalPowerInWatts") is not None,
        value_fn=lambda d: d.get("poePseConsumedPowerInWatts"),
    ),
    DeviceSensorDescription(
        key="poe_budget",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: d.get("poePseNominalPowerInWatts") is not None,
        value_fn=lambda d: d.get("poePseNominalPowerInWatts"),
    ),
)


@dataclass(frozen=True, kw_only=True)
class RadioSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


RADIO_SENSORS: tuple[RadioSensorDescription, ...] = (
    RadioSensorDescription(
        key="utilization",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.get("utilizationPercent"),
    ),
    RadioSensorDescription(
        key="clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.get("wirelessClientsCount"),
    ),
    RadioSensorDescription(
        key="channel",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.get("channel"),
    ),
    RadioSensorDescription(
        key="tx_power",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.get("txPowerEirpInDbm"),
    ),
)


@dataclass(frozen=True, kw_only=True)
class PortSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True


PORT_SENSORS: tuple[PortSensorDescription, ...] = (
    PortSensorDescription(
        key="speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=0,
        value_fn=lambda p: SPEED_MBPS.get(p.get("speed")) if p.get("isLinkUp") else 0,
    ),
    PortSensorDescription(
        key="poe_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        exists_fn=lambda p: bool(p.get("isPoeSupported")),
        value_fn=lambda p: (p.get("powerProvidedInMilliwatts") or 0) / 1000,
    ),
    PortSensorDescription(
        key="downstream_data",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda p: p.get("downstreamDataTransferredInBytes"),
    ),
    PortSensorDescription(
        key="upstream_data",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda p: p.get("upstreamDataTransferredInBytes"),
    ),
    PortSensorDescription(
        key="downstream_throughput",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: p.get("downstreamThroughputInBitsPerSecond"),
    ),
    PortSensorDescription(
        key="upstream_throughput",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: p.get("upstreamThroughputInBitsPerSecond"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InstantOnConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    fast = rt.coordinator
    port_sensors = entry.options.get(CONF_PORT_SENSORS, False)
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new: list[SensorEntity] = []
        for site_id, data in (fast.data or {}).items():
            if site_id not in known:
                known.add(site_id)
                new += [
                    SiteSensor(rt.slow if d.slow else fast, fast, site_id, d) for d in SITE_SENSORS
                ]
            for dev_id, dev in data.devices.items():
                if dev_id not in known:
                    known.add(dev_id)
                    new.append(BootTimeSensor(fast, site_id, dev_id))
                    new += [
                        DeviceSensor(fast, site_id, dev_id, d) for d in DEVICE_SENSORS if d.exists_fn(dev)
                    ]
                for radio in dev.get("radios") or []:
                    rkey = f"{dev_id}_radio_{radio.get('band')}"
                    if radio.get("band") and rkey not in known:
                        known.add(rkey)
                        new += [RadioSensor(fast, site_id, dev_id, radio["band"], d) for d in RADIO_SENSORS]
                if not port_sensors:
                    continue
                for port in ports_of(dev):
                    pkey = f"{dev_id}_port_{port['portNumber']}"
                    if pkey not in known:
                        known.add(pkey)
                        new += [
                            PortSensor(fast, site_id, dev_id, port["portNumber"], d)
                            for d in PORT_SENSORS
                            if d.exists_fn(port)
                        ]
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(fast.async_add_listener(add_new))


class SiteSensor(SiteEntity, SensorEntity):
    entity_description: SiteSensorDescription

    def __init__(self, coordinator, fast: InstantOnCoordinator, site_id: str, description: SiteSensorDescription) -> None:
        super().__init__(coordinator, fast, site_id, description.key)
        self.entity_description = description
        self._attr_translation_key = f"site_{description.key}"

    def _slow(self) -> dict[str, Any]:
        return (self.fast.config_entry.runtime_data.slow.data or {}).get(self.site_id, {}) \
            if self.entity_description.slow else {}

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.site_data, self._slow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.site_data, self._slow())


class DeviceSensor(DeviceEntity, SensorEntity):
    entity_description: DeviceSensorDescription

    def __init__(self, coordinator: InstantOnCoordinator, site_id: str, device_id: str, description: DeviceSensorDescription) -> None:
        super().__init__(coordinator, site_id, device_id, description.key)
        self.entity_description = description
        self._attr_translation_key = f"device_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.device)


class BootTimeSensor(DeviceEntity, SensorEntity):
    """Last boot, derived from uptime; only moves when it shifts by more than a minute."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_last_boot"

    def __init__(self, coordinator: InstantOnCoordinator, site_id: str, device_id: str) -> None:
        super().__init__(coordinator, site_id, device_id, "last_boot")
        self._boot: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        uptime = (self.device or {}).get("uptimeInSeconds")
        if uptime is None:
            return self._boot
        boot = (dt_util.utcnow() - timedelta(seconds=uptime)).replace(second=0, microsecond=0)
        if self._boot is None or abs((boot - self._boot).total_seconds()) > 90:
            self._boot = boot
        return self._boot


class RadioSensor(DeviceEntity, SensorEntity):
    entity_description: RadioSensorDescription

    def __init__(self, coordinator: InstantOnCoordinator, site_id: str, device_id: str, band: str, description: RadioSensorDescription) -> None:
        super().__init__(coordinator, site_id, device_id, f"radio_{band}_{description.key}")
        self.entity_description = description
        self.band = band
        self._attr_translation_key = f"radio_{description.key}"
        self._attr_translation_placeholders = {"band": BAND_LABEL.get(band, band)}

    @property
    def radio(self) -> dict[str, Any] | None:
        return next((r for r in (self.device or {}).get("radios") or [] if r.get("band") == self.band), None)

    @property
    def available(self) -> bool:
        return super().available and self.radio is not None

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.radio or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "channel" or not self.radio:
            return None
        return {"channel_width": self.radio.get("channelWidth"), "radio_mac": self.radio.get("id")}


class PortSensor(PortEntity, SensorEntity):
    entity_description: PortSensorDescription

    def __init__(self, coordinator: InstantOnCoordinator, site_id: str, device_id: str, port_number: int, description: PortSensorDescription) -> None:
        super().__init__(coordinator, site_id, device_id, port_number, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.port or {})
