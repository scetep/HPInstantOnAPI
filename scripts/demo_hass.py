#!/usr/bin/env python3
"""Run Home Assistant with the integration backed by the anonymized test fixtures.

For UI development and screenshots; no Instant On account needed.

  python3 scripts/demo_hass.py /tmp/ha-demo        # then open http://127.0.0.1:18124

Sign in to the integration with any email/password; the verification code is
123456. A few clients are also registered as "other integration" devices so the
map shows the Home Assistant badge.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
OTP = "123456"

SWITCH_NAMES = ["Core Switch", "Office Switch"]
AP_NAMES = ["Living Room AP", "Kitchen AP", "Upstairs AP", "Garage AP", "Hallway AP", "Patio AP"]
CLIENT_NAMES = [
    "Living Room TV", "Kitchen Display", "Office PC", "NAS", "Camera Recorder", "Game Console",
    "Thermostat", "Doorbell", "Garage Door", "Printer", "Robot Vacuum", "Smart Speaker",
    "Work Laptop", "Tablet", "Phone (Alex)", "Phone (Sam)", "Media Player", "Smart Plug 1",
    "Smart Plug 2", "Smart Plug 3", "Dishwasher", "Washer", "Home Assistant", "Weather Station",
    "Bedroom Speaker", "Kids Tablet", "E-Reader", "Watch", "Car", "Solar Inverter",
    "Air Purifier", "Desk Lamp", "Office Speaker", "Hallway Sensor", "Doorbell Chime",
    "Security Hub", "Baby Monitor", "Garden Sprinkler", "Light Strip", "Fridge", "Oven",
]
SSID_NAMES = ["Home", "Home-Kids", "Home-IoT", "Guest", "Cameras", "Office"]
# Clients that get a matching device from "another integration" in the demo.
HA_KNOWN = ["Garage Door", "Thermostat", "Home Assistant", "Doorbell", "Weather Station", "Solar Inverter"]


def _load() -> dict:
    return {f.stem: json.loads(f.read_text()) for f in FIXTURES.glob("*.json")}


def _friendly(data: dict) -> dict:
    """Replace fixture placeholders ("Device 3", "Client 12", "Network 1") with readable names."""
    mapping: dict[str, str] = {"Test Site": "Demo Home"}
    devices = sorted(data["inventory"]["elements"], key=lambda d: (d["deviceType"] != "switch", -len(d.get("ethernetPorts") or [])))
    switches = iter(SWITCH_NAMES)
    aps = iter(AP_NAMES)
    for dev in devices:
        mapping[dev["name"]] = next(switches if dev["deviceType"] == "switch" else aps)
    # Several fixture clients share a placeholder name; give each MAC its own.
    names = iter(CLIENT_NAMES)
    for client in data["clientSummary"]["elements"]:
        name = client.get("name") or ""
        if name.startswith("Client "):
            client["name"] = next(names, name)
            mapping.setdefault(name, client["name"])
    ssids = iter(SSID_NAMES)

    def walk(obj):
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, str):
            if obj.startswith("Network ") and obj not in mapping:
                mapping[obj] = next(ssids, obj)
            return mapping.get(obj, obj)
        return obj

    return walk(data)


DATA = _friendly(_load())


def _patch() -> None:
    from custom_components.instant_on import api

    async def settings(self):
        return {
            "restApiUrl": "https://demo.invalid", "ssoFqdn": "https://demo.invalid",
            "ssoClientIdAuthZ": "demo", "ssoRedirectUrl": "https://demo.invalid",
        }

    async def login(self, username, password, otp=None):
        if otp != OTP:
            raise api.InstantOnMfaRequiredError("Invalid verification code" if otp else "Verification code required")
        return self._store_tokens({"access_token": "demo", "refresh_token": "demo-refresh", "expires_in": 1800})

    async def access_token(self, force_refresh=False):
        return "demo"

    async def revoke(self):
        return None

    async def get(self, path):
        path = path.strip("/")
        if path == "sites":
            return copy.deepcopy(DATA["sites"])
        return copy.deepcopy(DATA.get(path.split("/")[-1]))

    api.InstantOnAuth.settings = settings
    api.InstantOnAuth.login = login
    api.InstantOnAuth.access_token = access_token
    api.InstantOnAuth.revoke = revoke
    api.InstantOnClient.get = get


LINKS_COMPONENT = '''
"""Demo helper: registers a few client MACs as devices of another integration."""
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers import device_registry as dr

KNOWN = {known}


async def async_setup(hass, config):
    async def create(_event=None):
        entries = hass.config_entries.async_entries("sun")
        if not entries:
            return
        reg = dr.async_get(hass)
        for mac, name in KNOWN.items():
            reg.async_get_or_create(
                config_entry_id=entries[0].entry_id,
                connections={{(dr.CONNECTION_NETWORK_MAC, mac)}},
                name=name, manufacturer="Demo",
            )
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, create)
    return True
'''


def _prepare(config: Path) -> None:
    (config / "custom_components").mkdir(parents=True, exist_ok=True)
    link = config / "custom_components" / "instant_on"
    if not link.exists():
        link.symlink_to(REPO / "custom_components" / "instant_on")
    known = {
        c["macAddress"]: c["name"]
        for c in DATA["clientSummary"]["elements"]
        if c.get("name") in HA_KNOWN
    }
    helper = config / "custom_components" / "demo_links"
    helper.mkdir(exist_ok=True)
    (helper / "__init__.py").write_text(LINKS_COMPONENT.format(known=repr(known)))
    (helper / "manifest.json").write_text(json.dumps({
        "domain": "demo_links", "name": "Demo links", "version": "0.0.0",
        "codeowners": [], "dependencies": [], "documentation": "https://example.invalid", "requirements": [],
    }))
    cfg = config / "configuration.yaml"
    if not cfg.exists():
        # No default_config: its network discovery would show real LAN devices in screenshots.
        cfg.write_text(
            "frontend:\nconfig:\nhistory:\nlogbook:\nmy:\nsun:\nperson:\nsearch:\nsystem_health:\n"
            "demo_links:\n"
            "http:\n  server_host: 127.0.0.1\n  server_port: 18124\n"
            "logger:\n  default: warning\n"
        )


def main() -> None:
    config = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ha-demo").resolve()
    _prepare(config)
    sys.path.insert(0, str(config))
    _patch()
    from homeassistant.__main__ import main as hass_main

    sys.argv = ["hass", "-c", str(config), "--skip-pip-packages", "instant_on,demo_links"]
    sys.exit(hass_main())


if __name__ == "__main__":
    main()
