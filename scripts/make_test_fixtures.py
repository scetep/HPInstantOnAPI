#!/usr/bin/env python3
"""Turn a CLI data dump into anonymized test fixtures.

  python3 cli/instanton.py            # writes output/latest/<site-id>/*.json
  python3 scripts/make_test_fixtures.py output/latest/<site-id> tests/fixtures

MAC addresses, IPs, UUIDs, serials, names, SSIDs, emails and locations are
replaced with stable fake values, so the fixtures can be committed publicly.
Community members can use the same script to contribute data from hardware we
don't have (gateways, stacks, mesh...).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

FILES = {
    "site.json": None,
    "inventory.json": None,
    "clientSummary.json": None,
    "events.json": 25,  # keep only the newest N events
    "landingPage.json": None,
    "maintenance.json": None,
    "networksSummary.json": None,
    "wiredNetworks.json": None,
}

MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
IP_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")

NAME_KEYS = {
    "name", "siteName", "deviceName", "clientName", "uplinkDeviceName", "directlyConnectedDeviceName",
    "parentName", "stackName", "hostName", "defaultName",
}
SSID_KEYS = {"networkName", "networkSsid", "wirelessNetworkName", "wiredNetworkName"}
SECRET_KEYS = {"preSharedKey", "serialNumber", "sfpSerialNumber", "email", "recoveryEmail", "userId"}
DROP_KEYS = {"userPermissions"}
VERSION_KEYS = {"currentDrtVersion", "currentCpldVersion", "newUpdateVersion", "currentVersion"}


def h(value: str, n: int = 12) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:n]


class Anonymizer:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.ssids: dict[str, str] = {}
        self.subnets: dict[str, str] = {}

    def mac(self, m: re.Match) -> str:
        d = h(m.group(0).lower())
        return "02:" + ":".join(d[i:i + 2] for i in range(0, 10, 2))

    def ip(self, m: re.Match) -> str:
        a, b, c, d = m.groups()
        if a in ("255", "0"):
            return m.group(0)
        prefix = f"{a}.{b}.{c}"
        if prefix not in self.subnets:
            self.subnets[prefix] = f"192.168.{len(self.subnets) + 10}"
        return f"{self.subnets[prefix]}.{d}"

    def uuid(self, m: re.Match) -> str:
        d = h(m.group(0), 32)
        return f"{d[:8]}-{d[8:12]}-4{d[13:16]}-8{d[17:20]}-{d[20:32]}"

    def name(self, value: str, device_like: bool) -> str:
        if MAC_RE.fullmatch(value):
            return MAC_RE.sub(self.mac, value)
        if value not in self.names:
            self.names[value] = f"{'Device' if device_like else 'Client'} {len(self.names) + 1}"
        return self.names[value]

    def ssid(self, value: str) -> str:
        if value not in self.ssids:
            self.ssids[value] = f"Network {len(self.ssids) + 1}"
        return self.ssids[value]

    def string(self, value: str) -> str:
        if value in self.names:
            return self.names[value]
        if value in self.ssids:
            return self.ssids[value]
        value = EMAIL_RE.sub("user@example.com", value)
        value = MAC_RE.sub(self.mac, value)
        value = UUID_RE.sub(self.uuid, value)
        return IP_RE.sub(self.ip, value)

    def walk(self, obj, key: str = "", device_like: bool = False):
        if isinstance(obj, dict):
            is_dev = obj.get("kind") in ("deviceSummary", "site", "uplinkSummary") or "deviceType" in obj
            out = {}
            for k, v in obj.items():
                if k in DROP_KEYS:
                    out[k] = []
                elif k in SECRET_KEYS and v:
                    out[k] = "redacted" if k != "email" else "user@example.com"
                elif k in ("latitude", "longitude") and v:
                    out[k] = "0.0"
                elif k == "address" and v:
                    out[k] = "Example Street 1, Example City"
                else:
                    out[k] = self.walk(v, k, is_dev or k in ("deviceName", "uplinkDeviceName"))
            return out
        if isinstance(obj, list):
            return [self.walk(v, key, device_like) for v in obj]
        if isinstance(obj, str) and obj:
            if key in NAME_KEYS:
                return self.name(obj, device_like or key != "name")
            if key in SSID_KEYS:
                return self.ssid(obj)
            if key == "deviceNames":
                return self.name(obj, True)
            if "version" in key.lower() or key in VERSION_KEYS:
                return obj  # "3.4.0.0-6" looks like an IP address but isn't one
            return self.string(obj)
        return obj


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    anon = Anonymizer()
    raw = {}
    for name, limit in FILES.items():
        data = json.loads((src / name).read_text())
        if limit and isinstance(data, dict) and isinstance(data.get("elements"), list):
            data["elements"] = sorted(data["elements"], key=lambda e: -(e.get("occurrenceTime") or 0))[:limit]
        raw[name] = data
    site = raw["site.json"]
    anon.names[site.get("name", "")] = "Test Site"
    # Stable device names regardless of API ordering: biggest switch first, then by hashed MAC.
    devices = sorted(
        raw["inventory.json"]["elements"],
        key=lambda d: (d.get("deviceType") != "switch", -len(d.get("ethernetPorts") or []), h(d["id"].lower())),
    )
    counters: dict[str, int] = {}
    for dev in devices:
        label = "Switch" if dev.get("deviceType") == "switch" else "AP" if dev.get("deviceType") == "accessPoint" else "Device"
        counters[label] = counters.get(label, 0) + 1
        anon.names[dev["name"]] = f"{label} {counters[label]}"
    # Name devices first so references elsewhere reuse "Device N".
    order = ["inventory.json", "site.json"] + [n for n in FILES if n not in ("inventory.json", "site.json")]
    for name in order:
        out = anon.walk(raw[name])
        if name == "site.json":
            out = {"kind": "resourceList", "elements": [out]}
            name = "sites.json"
        (dst / name).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(FILES)} fixtures to {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
