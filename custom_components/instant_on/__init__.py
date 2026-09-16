"""HPE Aruba Networking Instant On (unofficial cloud API) integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import (
    InstantOnAuth,
    InstantOnAuthError,
    InstantOnClient,
    InstantOnError,
)
from .const import ATTR_MAC, CONF_REFRESH_TOKEN, DOMAIN, SERVICE_LOOKUP_MAC, SIGNAL_ENTRIES_CHANGED
from .coordinator import InstantOnCoordinator, InstantOnSlowCoordinator
from .panel import async_register_panel, async_setup_panel_support, async_unregister_panel

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

LOOKUP_MAC_SCHEMA = vol.Schema({vol.Required(ATTR_MAC): cv.string})


@dataclass
class InstantOnRuntimeData:
    client: InstantOnClient
    coordinator: InstantOnCoordinator
    slow: InstantOnSlowCoordinator


type InstantOnConfigEntry = ConfigEntry[InstantOnRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the MAC lookup service."""

    async def lookup_mac(call: ServiceCall) -> ServiceResponse:
        mac = call.data[ATTR_MAC]
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            if found := entry.runtime_data.coordinator.find_mac(mac):
                return found
        return {"found": False, "mac": mac.lower()}

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOOKUP_MAC,
        lookup_mac,
        schema=LOOKUP_MAC_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    await async_setup_panel_support(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: InstantOnConfigEntry) -> bool:
    session = async_create_clientsession(hass)

    @callback
    def save_refresh_token(token: str) -> None:
        # Refresh tokens rotate on every use; persist the latest one.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    auth = InstantOnAuth(session, entry.data[CONF_REFRESH_TOKEN], save_refresh_token)
    client = InstantOnClient(session, auth)
    try:
        # Refresh tokens rotate and a replayed one revokes the whole session, so
        # every rotation is persisted immediately via save_refresh_token.
        await auth.access_token()
    except InstantOnAuthError as err:
        raise ConfigEntryAuthFailed(
            f"Sign-in for {entry.data[CONF_USERNAME]} expired: {err}"
        ) from err
    except InstantOnError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = InstantOnCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    slow = InstantOnSlowCoordinator(hass, entry, client, coordinator)
    await slow.async_config_entry_first_refresh()

    entry.runtime_data = InstantOnRuntimeData(client, coordinator, slow)
    # The options flow reloads the entry itself; token rotation must not.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_panel(hass, (await async_get_integration(hass, DOMAIN)).version)
    async_dispatcher_send(hass, SIGNAL_ENTRIES_CHANGED, True)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InstantOnConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        if not [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.entry_id != entry.entry_id]:
            async_unregister_panel(hass)
        # Runs once the entry is marked unloaded, so listeners detach from it.
        hass.loop.call_soon(async_dispatcher_send, hass, SIGNAL_ENTRIES_CHANGED, False)
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: InstantOnConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting devices (APs, switches, sites) that Instant On no longer reports."""
    data = entry.runtime_data.coordinator.data or {}
    known = set(data) | {dev_id for site in data.values() for dev_id in site.devices}
    return not any(domain == DOMAIN and ident in known for domain, ident in device.identifiers)


async def async_remove_entry(hass: HomeAssistant, entry: InstantOnConfigEntry) -> None:
    """Revoke the refresh token when the integration is deleted."""
    if not (token := entry.data.get(CONF_REFRESH_TOKEN)):
        return
    auth = InstantOnAuth(async_create_clientsession(hass), token)
    try:
        await auth.revoke()
    except InstantOnError as err:
        _LOGGER.debug("Could not revoke refresh token: %s", err)
