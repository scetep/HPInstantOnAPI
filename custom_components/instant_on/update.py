"""Site firmware update entity (Instant On updates all devices of a site together)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import InstantOnConfigEntry
from .entity import SiteEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InstantOnConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [
            SiteFirmwareUpdate(rt.slow, rt.coordinator, site_id, "firmware")
            for site_id in (rt.coordinator.data or {})
            if site_id not in known
        ]
        known.update(e.site_id for e in new)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(rt.coordinator.async_add_listener(add_new))


class SiteFirmwareUpdate(SiteEntity, UpdateEntity):
    _attr_title = "Instant On firmware"

    @property
    def _maintenance(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self.site_id, {}).get("maintenance") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._maintenance)

    @property
    def installed_version(self) -> str | None:
        return self._maintenance.get("currentVersion")

    @property
    def latest_version(self) -> str | None:
        m = self._maintenance
        if m.get("newUpdateVersion"):
            return m["newUpdateVersion"]
        if m.get("notUpToDateOnlineDeviceCount") or m.get("notUpToDateOfflineDeviceCount"):
            return f"{m.get('currentVersion')} (devices pending)"
        return m.get("currentVersion")

    @property
    def in_progress(self) -> bool:
        return bool(self._maintenance.get("updateInProgress"))

    @property
    def update_percentage(self) -> int | None:
        return self._maintenance.get("updatePercentage") if self.in_progress else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._maintenance
        return {
            "state": m.get("state"),
            "maintenance_window": f"{m.get('day')} {m.get('startTime')}",
            "update_expected": m.get("updateExpectedDateTime"),
            "update_deadline": m.get("updateDeadline"),
            "devices_up_to_date": m.get("upToDateDeviceCount"),
            "devices_total": m.get("totalDeviceCount"),
        }
