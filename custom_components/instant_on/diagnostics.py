"""Diagnostics download (redacted) — attach this to bug reports."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import InstantOnConfigEntry
from .const import CONF_REFRESH_TOKEN

TO_REDACT = {
    CONF_USERNAME,
    CONF_REFRESH_TOKEN,
    "preSharedKey",
    "passphrase",
    "password",
    "secret",
    "sharedSecret",
    "token",
    "deviceWebUiToken",
    "supportToken",
    "email",
    "recoveryEmail",
    "address",
    "latitude",
    "longitude",
    "serialNumber",
    "defaultName",
    "userId",
    "hostName",
    "name",
    "siteName",
    "clientName",
    "deviceName",
    "deviceNames",
    "networkName",
    "networkSsid",
    "wirelessNetworkName",
    "wiredNetworkName",
    "uplinkDeviceName",
    "directlyConnectedDeviceName",
    "valueStr",
    "account",
}
MAC_RE = re.compile(r"\b([0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b")


def _pseudonymize(obj: Any) -> Any:
    """Hash MACs and mask the last IPv4 octet so structure stays useful but anonymous."""
    if isinstance(obj, dict):
        return {k: _pseudonymize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_pseudonymize(v) for v in obj]
    if isinstance(obj, str):
        obj = MAC_RE.sub(lambda m: "02:" + ":".join(
            hashlib.sha256(m.group(0).lower().encode()).hexdigest()[i:i + 2] for i in range(0, 10, 2)
        ), obj)
        obj = EMAIL_RE.sub("**REDACTED**", obj)
        return IP_RE.sub(r"\1.x", obj)
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: InstantOnConfigEntry
) -> dict[str, Any]:
    rt = entry.runtime_data
    sites = {
        site_id: {
            "site": data.site,
            "inventory": list(data.devices.values()),
            "clientSummary": list(data.clients.values()),
            "events": data.events[:50],
            **(rt.slow.data or {}).get(site_id, {}),
        }
        for site_id, data in (rt.coordinator.data or {}).items()
    }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "sites": _pseudonymize(async_redact_data(sites, TO_REDACT)),
    }
