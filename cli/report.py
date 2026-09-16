"""
Markdown report rendering for instanton.py.

  status_report(sites)  -> status.md       health overview with Mermaid diagrams
  full_report(sites)    -> full-report.md  everything the API returned, per section

`sites` is the list produced by instanton.collect_site() (one dict per site with
"site", "endpoints" and "errors"), after redact() has been applied.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SECRET_KEY_RE = re.compile(r"(presharedkey|passphrase|password|secret|token)$", re.I)
REDACTED = "«redacted»"


# --------------------------------------------------------------------------- helpers

def redact(obj):
    """Recursively blank out values whose key looks like a secret."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if SECRET_KEY_RE.search(k) and v not in (None, "") else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _elements(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("elements"), list):
        return data["elements"]
    return []


def _cell(v) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, bool):
        return "✅" if v else "–"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, separators=(",", ":"))
        if len(v) > 120:
            v = v[:117] + "…"
    return str(v).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def kv_table(d: dict, skip: tuple = ("kind",)) -> str:
    rows = [[_label(k), v] for k, v in d.items() if k not in skip]
    return table(["Field", "Value"], rows)


def _label(key: str) -> str:
    """camelCase -> 'Camel case'"""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", key).replace("_", " ")
    return s[:1].upper() + s[1:].lower() if s else s


def human_bytes(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_bps(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if abs(n) < 1000 or unit == "Gbps":
            return f"{n:.0f} {unit}" if unit == "bps" else f"{n:.1f} {unit}"
        n /= 1000


def human_duration(s) -> str:
    if s is None:
        return "–"
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def human_speed(s) -> str:
    return {"mbps10": "10M", "mbps100": "100M", "mbps1000": "1G", "mbps2500": "2.5G",
            "mbps5000": "5G", "mbps10000": "10G"}.get(s, s or "–")


class Clock:
    def __init__(self, tz_name: str | None):
        try:
            self.tz = ZoneInfo(tz_name) if tz_name else UTC
        except Exception:
            self.tz = UTC

    def fmt(self, epoch) -> str:
        if not epoch:
            return "–"
        return datetime.fromtimestamp(epoch, self.tz).strftime("%Y-%m-%d %H:%M")

    def now(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d %H:%M %Z")


HEALTH_ICON = {"good": "🟢", "fair": "🟡", "warning": "🟡", "poor": "🔴", "critical": "🔴", "down": "🔴"}


def icon(health) -> str:
    return HEALTH_ICON.get(str(health).lower(), "⚪")


def client_name(c: dict) -> str:
    return c.get("name") or c.get("hostName") or c.get("macAddress") or "?"


def client_network(c: dict):
    """Wired clients carry their network under connectedToPorts[].accessedWiredNetworks."""
    name = c.get("wirelessNetworkName") or c.get("wiredNetworkName")
    if not name:
        nets = {n.get("networkName") for p in c.get("connectedToPorts") or []
                for n in p.get("accessedWiredNetworks") or [] if n.get("networkName")}
        name = ", ".join(sorted(nets)) or None
    return name


def _mid(raw_id: str) -> str:
    """Mermaid-safe node id."""
    return "n_" + re.sub(r"[^A-Za-z0-9]", "", str(raw_id))


def _mtext(s) -> str:
    """Mermaid-safe label text (used inside double quotes)."""
    return str(s).replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _site_parts(data: dict):
    eps = data["endpoints"]
    site = data["site"]
    devices = _elements(eps.get("inventory"))
    clients = _elements(eps.get("clientSummary"))
    online = [c for c in clients if c.get("status") == "up"]
    offline = [c for c in clients if c.get("status") != "up"]
    clock = Clock(site.get("timezoneIana") or (eps.get("timezone") or {}).get("timezoneIana"))
    return site, eps, devices, online, offline, clock


def _collect_alerts(site: dict, devices: list, clients: list) -> list[dict]:
    """Active alerts are scattered: site.latestActiveAlert plus per-device/per-client lists."""
    seen, out = set(), []

    def add(a, subject):
        if not a or a.get("id") in seen or a.get("clearedTime"):
            return
        seen.add(a.get("id"))
        props = a.get("alertTypeProperties") or {}
        out.append({
            "severity": a.get("severity"),
            "type": a.get("type"),
            "subject": subject or props.get("clientName") or ", ".join(props.get("deviceNames") or []) or props.get("wanName"),
            "raised": a.get("raisedTime"),
        })

    for dev in devices:
        for a in dev.get("activeAlerts") or []:
            add(a, dev.get("name"))
    for c in clients:
        for a in c.get("activeAlerts") or []:
            add(a, client_name(c))
    add(site.get("latestActiveAlert"), None)
    order = {"major": 0, "minor": 1, "info": 2}
    return sorted(out, key=lambda a: order.get(a["severity"], 9))


def _port_to_child(devices: list) -> dict:
    """(parent_id, child_id) -> 'port N · speed' using the parent's port table."""
    labels = {}
    for dev in devices:
        for p in dev.get("ethernetPorts") or []:
            child = p.get("directlyConnectedDeviceId")
            if child:
                labels[(dev["id"], child)] = f"port {p.get('portNumber')} · {human_speed(p.get('speed'))}"
    return labels


# --------------------------------------------------------------------------- status.md

def _topology(devices: list, online_clients: list) -> str:
    by_id = {d["id"]: d for d in devices}
    port_labels = _port_to_child(devices)
    wireless = Counter(c.get("deviceId") for c in online_clients if c.get("wirelessNetworkId"))
    wired = Counter()
    for c in online_clients:
        for p in c.get("connectedToPorts") or []:
            wired[p.get("deviceId")] += 1

    lines = ["```mermaid", "flowchart TD"]
    gateways = {d.get("defaultGateway") for d in devices if d.get("defaultGateway")}
    if gateways:
        gw = ", ".join(sorted(gateways))
        lines.append(f'  GW(["🌐 Upstream gateway<br/>{_mtext(gw)}"])')

    for d in devices:
        kind = "📡" if d.get("deviceType") == "accessPoint" else "🔀"
        counts = []
        if wireless[d["id"]]:
            counts.append(f"📶 {wireless[d['id']]}")
        if wired[d["id"]]:
            counts.append(f"🔌 {wired[d['id']]}")
        label = (f"{kind} <b>{_mtext(d.get('name'))}</b><br/>{_mtext(d.get('model'))} · {_mtext(d.get('ipAddress'))}"
                 f"<br/>{icon(d.get('health'))} {_mtext(d.get('status'))} · up {human_duration(d.get('uptimeInSeconds'))}")
        if counts:
            label += "<br/>" + " ".join(counts)
        shape_open, shape_close = ("[", "]") if d.get("deviceType") != "accessPoint" else ("(", ")")
        lines.append(f'  {_mid(d["id"])}{shape_open}"{label}"{shape_close}')

    for d in devices:
        parent = d.get("uplinkDeviceDeviceId")
        if parent in by_id:
            lbl = port_labels.get((parent, d["id"]))
            arrow = f'-- "{_mtext(lbl)}" -->' if lbl else "-->"
            lines.append(f"  {_mid(parent)} {arrow} {_mid(d['id'])}")
        elif gateways:
            up = next((p for p in d.get("ethernetPorts") or [] if p.get("isUplink")), None)
            lbl = f"uplink port {up.get('portNumber')} · {human_speed(up.get('speed'))}" if up else "uplink"
            lines.append(f'  GW -. "{_mtext(lbl)}" .-> {_mid(d["id"])}')

    lines += [
        "  classDef good fill:#d3f9d8,stroke:#2b8a3e,color:#1b4332",
        "  classDef warn fill:#fff3bf,stroke:#e67700,color:#5c3c00",
        "  classDef bad fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a",
        "  classDef gw fill:#e7f5ff,stroke:#1971c2,color:#0b3d6e",
    ]
    if gateways:
        lines.append("  class GW gw")
    for d in devices:
        cls = "bad" if d.get("status") != "up" else "good" if d.get("health") == "good" else "warn"
        lines.append(f"  class {_mid(d['id'])} {cls}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _pie(title: str, items: list[tuple[str, float]]) -> str:
    items = [(k, v) for k, v in items if v]
    if not items:
        return ""
    body = "\n".join(f'  "{_mtext(k if k is not None else "unknown")}" : {round(v, 2)}' for k, v in items)
    return f"```mermaid\npie showData\n  title {_mtext(title)}\n{body}\n```\n"


def _radio_chart(devices: list) -> str:
    labels, values = [], []
    for d in devices:
        for r in d.get("radios") or []:
            if r.get("utilizationPercent") is None:
                continue
            short = re.sub(r"\s*(WAP|AP)\b.*$", "", d.get("name") or "?").strip() or d.get("name")
            labels.append(f"{short} {r.get('band', '').replace('ghz', 'G')} ch{r.get('channel')}")
            values.append(r["utilizationPercent"])
    if not labels:
        return ""
    xs = ", ".join(f'"{_mtext(x)}"' for x in labels)
    return ("```mermaid\n"
            '%%{init: {"themeVariables": {"xyChart": {"plotColorPalette": "#1971c2"}}}}%%\n'
            "xychart-beta horizontal\n"
            '  title "Radio channel utilization (%)"\n'
            f"  x-axis [{xs}]\n"
            '  y-axis "Utilization %" 0 --> 100\n'
            f"  bar [{', '.join(str(v) for v in values)}]\n"
            "```\n")


def status_report(sites: list[dict]) -> str:
    out = ["# 📡 Instant On network status", ""]
    for data in sites:
        site, eps, devices, online, offline, clock = _site_parts(data)
        health = eps.get("systemHealth") or {}
        landing = eps.get("landingPage") or {}
        maint = eps.get("maintenance") or {}
        alerts = _collect_alerts(site, devices, _elements(eps.get("clientSummary")))
        down = [d for d in devices if d.get("status") != "up"]
        score = (site.get("currentHealthScore") or {}).get("score")

        out += [
            f"## {icon(site.get('health'))} {site.get('name')}",
            "",
            f"_Generated {clock.now()}_",
            "",
            "| Site health | Score | Devices up | Clients online | Active alerts | Throughput now | Data (24h) | Firmware |",
            "|---|---|---|---|---|---|---|---|",
            f"| {icon(site.get('health'))} **{site.get('health')}** ({_label(site.get('healthReason') or '')}) "
            f"| {_cell(score)} "
            f"| {len(devices) - len(down)} / {len(devices)} "
            f"| {len(online)} "
            f"| {site.get('activeAlertsCount', 0)} "
            f"| {human_bps(health.get('instantThroughputInBitsPerSecond') or landing.get('currentNetworkThroughputInBitsPerSecond'))} "
            f"| {human_bytes(landing.get('totalDataTransferredDuringLast24HoursInBytes'))} "
            f"| {_cell(maint.get('currentVersion'))} ({maint.get('upToDateDeviceCount', '?')}/{maint.get('totalDeviceCount', '?')} current) |",
            "",
        ]

        if alerts or down:
            out.append("> [!WARNING]")
            for d in down:
                out.append(f"> **Device {d.get('status')}:** {d.get('name')} ({d.get('model')}, {d.get('ipAddress')})  ")
            for a in alerts:
                out.append(f"> **{str(a['severity']).upper()}** · {_label(a['type'] or '')} · {a['subject']} · since {clock.fmt(a['raised'])}  ")
            out.append("")
        else:
            out += ["> [!TIP]", "> All devices up, no active alerts.", ""]

        out += ["### Topology", "", "📡 access point · 🔀 switch · 📶 wireless clients · 🔌 wired clients", "",
                _topology(devices, online)]

        out += ["### Devices", ""]
        out.append(table(
            ["", "Name", "Model", "IP", "Status", "Uptime", "Firmware", "Up to date", "Uplink", "Power"],
            [[icon(d.get("health") if d.get("status") == "up" else "down"), d.get("name"), d.get("model"),
              d.get("ipAddress"), d.get("status"), human_duration(d.get("uptimeInSeconds")),
              d.get("currentFirmwareVersion"), d.get("isUpToDate"), d.get("uplinkDeviceName"),
              (f"PoE budget {d['poePseConsumedPowerInWatts']}/{d['poePseNominalPowerInWatts']} W"
               if d.get("poePseNominalPowerInWatts") else d.get("inputPowerSource"))]
             for d in sorted(devices, key=lambda x: (x.get("deviceType") != "switch", x.get("name") or ""))]
        ))

        out += ["### Clients", ""]
        by_conn = Counter()
        for c in online:
            by_conn["Wired" if not c.get("wirelessNetworkId") else
                    f"Wi-Fi {str(c.get('wirelessBand') or '?').replace('ghz', ' GHz')}"] += 1
        out.append(_pie(f"Online clients by connection ({len(online)})", sorted(by_conn.items())))
        per_ap = Counter(c.get("deviceName") for c in online if c.get("wirelessNetworkId"))
        out.append(_pie("Wireless clients per access point", per_ap.most_common()))
        per_net = Counter(client_network(c) for c in online)
        out.append(_pie("Online clients per network", per_net.most_common()))
        quality = Counter(c.get("signalQuality") for c in online if c.get("wirelessNetworkId"))
        out.append(_pie("Wireless signal quality", quality.most_common()))

        out += ["### Traffic", ""]
        cats = defaultdict(int)
        for u in _elements(eps.get("applicationCategoryUsage")):
            cats[u.get("applicationCategory")] += (u.get("downstreamDataTransferredDuringLast24HoursInBytes") or 0) + \
                                                  (u.get("upstreamDataTransferredDuringLast24HoursInBytes") or 0)
        top = sorted(cats.items(), key=lambda kv: -kv[1])
        head, rest = top[:8], sum(v for _, v in top[8:])
        items = [(_label(k), v / 1024 ** 3) for k, v in head] + ([("Other", rest / 1024 ** 3)] if rest else [])
        out.append(_pie("Top application categories, last 24h (GB)", items))

        talkers = sorted(online, key=lambda c: -(((c.get("dataTraffic") or {}).get("downstreamDataTransferredInBytesInLast24Hours") or 0)
                                                  + ((c.get("dataTraffic") or {}).get("upstreamDataTransferredInBytesInLast24Hours") or 0)))[:10]
        out += ["**Top 10 clients by data (24h)**", ""]
        out.append(table(
            ["Client", "Connection", "Down (24h)", "Up (24h)"],
            [[client_name(c), c.get("deviceName"),
              human_bytes((c.get("dataTraffic") or {}).get("downstreamDataTransferredInBytesInLast24Hours")),
              human_bytes((c.get("dataTraffic") or {}).get("upstreamDataTransferredInBytesInLast24Hours"))]
             for c in talkers]
        ))

        out += ["### Radios", "", _radio_chart(devices)]

        if offline:
            out += [f"### Recently offline clients ({len(offline)})", ""]
            out.append(table(
                ["Client", "Last seen on", "Network", "Offline since", "Watchlisted"],
                [[client_name(c), c.get("deviceName"), client_network(c),
                  clock.fmt(c.get("lastStateChange")), c.get("isWatchlisted")]
                 for c in sorted(offline, key=lambda c: -(c.get("lastStateChange") or 0))]
            ))

        out += ["### Maintenance", "",
                f"Firmware **{_cell(maint.get('currentVersion'))}**, state `{_cell(maint.get('state'))}`, "
                f"window **{_cell(maint.get('day'))} {_cell(maint.get('startTime'))}**, "
                f"next expected update {_cell(maint.get('updateExpectedDateTime'))}"
                + (f", **new version available: {maint['newUpdateVersion']}**" if maint.get("newUpdateVersion") else "")
                + ".", ""]

        if data["errors"]:
            out += ["### Collection errors", "", table(["Endpoint", "Error"], list(data["errors"].items()))]
    return "\n".join(out)


# --------------------------------------------------------------------------- full-report.md

def full_report(sites: list[dict]) -> str:
    out = ["# Instant On full data report", "",
           "Everything returned by the Instant On cloud API, grouped by source endpoint. "
           f"Secrets (keys, passwords, tokens) are shown as `{REDACTED}`.", ""]

    for data in sites:
        site, eps, devices, online, offline, clock = _site_parts(data)
        networks_by_id = {n.get("id"): n.get("wiredNetworkName") or n.get("networkName")
                          for n in _elements(eps.get("wiredNetworks")) + _elements(eps.get("networksSummary"))}
        dev_by_id = {d["id"]: d.get("name") for d in devices}

        out += [f"## Site: {site.get('name')}", "", f"_Generated {clock.now()}_", "",
                "**Contents:** [Site](#site) · [Health](#health) · [Devices](#devices) · "
                "[Wireless networks](#wireless-networks) · [Wired networks](#wired-networks--vlans) · "
                "[Clients](#clients) · [Application usage](#application-usage) · [Events](#events) · "
                "[Administration](#administration) · [Maintenance](#maintenance) · [Other endpoints](#other-endpoints)", ""]

        # ---- site
        out += ["### Site", "", "Source: `GET /sites`", ""]
        s = dict(site)
        perms = s.pop("userPermissions", None)
        caps = (s.pop("capabilities", None) or {}).get("capabilities")
        for k in ("latestAlert", "latestActiveAlert", "currentHealthScore", "activeAlertsCounters",
                  "detectedLocation", "configuredLocation"):
            v = s.pop(k, None)
            if isinstance(v, dict):
                if "address" in v:
                    s[k] = f"{v.get('address')} ({v.get('latitude')}, {v.get('longitude')})"
                elif k in ("latestAlert", "latestActiveAlert"):
                    s[k] = f"{v.get('severity')} {v.get('type')} raised {clock.fmt(v.get('raisedTime'))}"
                else:
                    s.update({f"{k}.{kk}": vv for kk, vv in v.items() if kk != "kind"})
        out.append(kv_table(s))
        if caps:
            out += ["", f"**Site capabilities:** {', '.join(f'`{c}`' for c in caps)}", ""]
        if perms:
            out += ["<details><summary>Account permissions on this site "
                    f"({len(perms)})</summary>", "", ", ".join(f"`{p}`" for p in perms), "", "</details>", ""]

        # ---- health
        out += ["### Health", "", "Source: `systemHealth`, `landingPage`, `alertsSummary`, `alerts`", ""]
        for ep in ("systemHealth", "landingPage", "alertsSummary"):
            if isinstance(eps.get(ep), dict):
                d = dict(eps[ep])
                for k in list(d):
                    if k.endswith("InBytes"):
                        d[k] = human_bytes(d[k])
                    elif k.endswith("InBitsPerSecond"):
                        d[k] = human_bps(d[k])
                    elif k.endswith("InSeconds"):
                        d[k] = human_duration(d[k])
                out += [f"**{ep}**", "", kv_table(d), ""]
        alerts = _collect_alerts(site, devices, _elements(eps.get("clientSummary")))
        out += ["**Active alerts**", "", table(["Severity", "Type", "Subject", "Raised"],
                                              [[a["severity"], a["type"], a["subject"], clock.fmt(a["raised"])] for a in alerts]),
                f"The `/alerts` list endpoint returned {len(_elements(eps.get('alerts')))} entries.", ""]

        # ---- devices
        out += ["### Devices", "", "Source: `inventory`", ""]
        for d in sorted(devices, key=lambda x: (x.get("deviceType") != "switch", x.get("name") or "")):
            out += [f"#### {icon(d.get('health'))} {d.get('name')} — {d.get('model')} ({d.get('deviceType')})", ""]
            general = {k: v for k, v in d.items()
                       if not isinstance(v, (dict, list)) and k not in ("kind",)}
            for k in list(general):
                if k.endswith("InSeconds"):
                    general[k] = f"{human_duration(general[k])} ({general[k]} s)"
            mgmt = d.get("managementIpAddress") or {}
            general["managementIp"] = f"{mgmt.get('ipAssignmentScheme')} {mgmt.get('staticIpAddress') or ''}".strip()
            general["wirelessNetworks"] = ", ".join(n.get("networkName") for n in d.get("wirelessNetworks") or []) or None
            general["healthConditions"] = d.get("healthConditions") or None
            general["activeAlerts"] = len(d.get("activeAlerts") or [])
            out += ["<details><summary>All device fields</summary>", "", kv_table(general), "</details>", ""]

            if d.get("radios"):
                out += ["**Radios**", "", table(
                    ["Band", "Channel", "Width", "Utilization", "Clients", "Tx power (EIRP)", "Reg. max", "Radio MAC"],
                    [[r.get("band"), r.get("channel"), r.get("channelWidth"),
                      f"{r.get('utilizationPercent')} %" if r.get("utilizationPercent") is not None else None,
                      r.get("wirelessClientsCount"),
                      f"{r.get('txPowerEirpInDbm')} dBm" if r.get("txPowerEirpInDbm") is not None else None,
                      f"{r.get('regulatoryMaxTxPowerEirpInDbm')} dBm" if r.get("regulatoryMaxTxPowerEirpInDbm") is not None else None,
                      r.get("id")] for r in d["radios"]]), ""]
            if d.get("radioManagementBands"):
                out += ["**Radio management**", "", table(
                    ["Band"] + sorted({k for v in d["radioManagementBands"].values() if isinstance(v, dict) for k in v}),
                    [[band] + [v.get(k) for k in sorted({k for v in d["radioManagementBands"].values() if isinstance(v, dict) for k in v})]
                     for band, v in d["radioManagementBands"].items() if isinstance(v, dict)]), ""]

            ports = d.get("ethernetPorts") or []
            if ports:
                out += ["**Ethernet ports**", "", table(
                    ["Port", "Name", "Link", "Speed", "Duplex", "Uplink", "Connected device", "Untagged VLAN",
                     "PoE", "PoE draw", "Down", "Up", "Throughput ↓/↑", "Flags"],
                    [[p.get("faceplatePortNumber") or p.get("portNumber"), p.get("name"),
                      "🟢 up" if p.get("isLinkUp") else ("⛔ disabled" if p.get("userDeactivated") else "down"),
                      human_speed(p.get("speed")) if p.get("isLinkUp") else None, p.get("duplex"),
                      p.get("isUplink"),
                      p.get("directlyConnectedDeviceName") or dev_by_id.get(p.get("directlyConnectedDeviceId")),
                      networks_by_id.get(p.get("customMappingUntaggedWiredNetworkId") or p.get("wiredNetworkId")),
                      p.get("poePseStatus") if p.get("isPoeSupported") else None,
                      f"{p['powerProvidedInMilliwatts'] / 1000:.1f} W" if p.get("powerProvidedInMilliwatts") else None,
                      human_bytes(p.get("downstreamDataTransferredInBytes")),
                      human_bytes(p.get("upstreamDataTransferredInBytes")),
                      f"{human_bps(p.get('downstreamThroughputInBitsPerSecond'))} / {human_bps(p.get('upstreamThroughputInBitsPerSecond'))}"
                      if p.get("isLinkUp") else None,
                      ", ".join(f for f, on in (("loop", p.get("isLoopDetected")), ("flapping", p.get("isLinkFlappingDetected")),
                                                ("bpdu-guard", p.get("isBpduGuardDetected")), ("bpdu-storm", p.get("isBpduStormDetected")),
                                                ("sfp", p.get("isSfp")), ("voice", p.get("isVoiceClientConnected"))) if on) or None]
                     for p in ports]), ""]
            trunks = [t for t in d.get("trunkPorts") or [] if t.get("directlyConnectedDeviceId") or t.get("portDataTraffic")]
            if trunks:
                out += ["**Trunks (LAGs)**", "", table(["Trunk", "Type", "Connected device"],
                                                       [[t.get("trunkNumber"), t.get("trunkType"), t.get("directlyConnectedDeviceName")] for t in trunks]), ""]
            if d.get("ipRoutes"):
                out += ["**IP routes**", "", table(["Route"], [[r] for r in d["ipRoutes"]]), ""]

        # ---- wireless networks
        out += ["### Wireless networks", "", "Source: `networksSummary`", ""]
        wnets = _elements(eps.get("networksSummary"))
        out.append(table(
            ["SSID", "Enabled", "Type", "Security", "Auth", "Key", "Hidden", "Bands", "VLAN / wired net",
             "Bandwidth limit", "Guest portal", "Schedule"],
            [[n.get("networkName"), n.get("isEnabled"), n.get("type"), n.get("security"), n.get("authentication"),
              n.get("preSharedKey"), n.get("isSsidHidden"), n.get("radioFrequencyBand"),
              n.get("vlanId") or networks_by_id.get(n.get("wiredNetworkId")),
              (f"{n.get('perClientBandwidthLimitInMbps')}↓/{n.get('perClientUploadBandwidthLimitInMbps')}↑ Mbps ({n.get('bandwidthLimitMode')})"
               if n.get("isBandwidthLimitEnabled") else None),
              n.get("isGuestPortalEnabled") or n.get("isCaptivePortalEnabled"),
              n.get("activeSchedule") or n.get("schedule")] for n in wnets]
        ))
        for n in wnets:
            flat = {k: v for k, v in n.items() if not isinstance(v, (dict, list))}
            out += [f"<details><summary>All fields: {n.get('networkName')}</summary>", "", kv_table(flat)]
            for k in ("qos", "capabilities", "radiusNasIpSettings"):
                if isinstance(n.get(k), dict):
                    out += [f"**{k}**", "", kv_table(n[k])]
            out += ["</details>", ""]

        # ---- wired networks
        out += ["### Wired networks / VLANs", "", "Source: `wiredNetworks`", ""]
        wired = _elements(eps.get("wiredNetworks"))
        out.append(table(
            ["Name", "VLAN", "Enabled", "Type", "Mgmt", "DHCP scope", "DNS", "Internet", "Inter-client", "IP routing",
             "Health", "Wired clients"],
            [[n.get("wiredNetworkName"), n.get("vlanId"), n.get("isEnabled"), n.get("type"), n.get("isManagement"),
              (f"{(n.get('dhcpScope') or {}).get('network')}/{(n.get('dhcpScope') or {}).get('netmask')}"
               if n.get("useDhcpScope") else "external"),
              ", ".join(filter(None, [((n.get("dhcpScope") or {}).get("dns") or {}).get(k) for k in
                                      ("customPrimaryDns", "customSecondaryDns", "automaticPrimaryDns", "automaticSecondaryDns")])) or None,
              n.get("isInternetAllowed"), n.get("isIntraSubnetTrafficAllowed"), n.get("isIpRoutingEnabled"),
              n.get("health"), n.get("wiredClientsCount")] for n in wired]
        ))
        for n in wired:
            maps = []
            for dm in n.get("devicePortMappings") or []:
                for pm in dm.get("portMappings") or []:
                    if pm.get("mapping") not in (None, "forbidden"):
                        maps.append([dev_by_id.get(dm.get("deviceId"), dm.get("deviceId")), pm.get("portNumber"), pm.get("mapping")])
                for tm in dm.get("trunkMappings") or []:
                    if tm.get("mapping") not in (None, "forbidden"):
                        maps.append([dev_by_id.get(dm.get("deviceId"), dm.get("deviceId")), f"trunk {tm.get('trunkNumber')}", tm.get("mapping")])
            reservations = (n.get("dhcpScope") or {}).get("ipReservations") or []
            flat = {k: v for k, v in n.items() if not isinstance(v, (dict, list))}
            out += [f"<details><summary>{n.get('wiredNetworkName')} (VLAN {n.get('vlanId')}): "
                    f"{len(maps)} port assignments, {len(reservations)} DHCP reservations</summary>", "",
                    kv_table(flat), "", "**Port assignments**", "", table(["Device", "Port", "Mapping"], maps)]
            if reservations:
                out += ["", "**DHCP reservations**", "", table(list(reservations[0].keys()), [list(r.values()) for r in reservations])]
            out += ["</details>", ""]

        # DHCP reservation info lives under clientSummary.metaData.networkScopes
        scopes = ((eps.get("clientSummary") or {}).get("metaData") or {}).get("networkScopes") or []
        res_rows = []
        for sc in scopes:
            info = sc.get("ipReservationInfo") or {}
            for c in (info.get("clients") or []) + (info.get("siteDevices") or []):
                res_rows.append([networks_by_id.get(sc.get("networkId"), sc.get("networkId")), c.get("name"),
                                 c.get("macAddress"), c.get("ipAddress"), c.get("isOnline"), c.get("hasActiveLease")])
        if res_rows:
            out += ["**Known IP assignments per network** (from `clientSummary.metaData`)", "",
                    table(["Network", "Name", "MAC", "IP", "Online", "Active lease"], res_rows), ""]

        # ---- clients
        out += ["### Clients", "", "Source: `clientSummary`", "",
                f"#### Online ({len(online)})", ""]
        rows = []
        for c in sorted(online, key=lambda c: (bool(c.get("wirelessNetworkId")), c.get("deviceName") or "", c.get("name") or "")):
            dt = c.get("dataTraffic") or {}
            ports = c.get("connectedToPorts") or []
            where = (f"{c.get('deviceName')} · {str(c.get('wirelessBand') or '').replace('ghz', ' GHz')}"
                     if c.get("wirelessNetworkId") else
                     "; ".join(f"{p.get('deviceName')} port {p.get('portName')} ({human_speed(p.get('portSpeed'))})" for p in ports)
                     or c.get("deviceName"))
            rows.append([icon(c.get("health")), client_name(c), c.get("ipAddress"), c.get("macAddress"),
                         client_network(c), c.get("vlanId"), where,
                         (f"{c.get('signalInDbm')} dBm / SNR {c.get('snrInDb')} ({c.get('signalQuality')})"
                          if c.get("wirelessNetworkId") else None),
                         f"{c.get('healthInPercent')} %" if c.get("healthInPercent") is not None else c.get("health"),
                         human_duration(c.get("connectionDurationInSeconds")),
                         human_bytes(dt.get("downstreamDataTransferredInBytesInLast24Hours")),
                         human_bytes(dt.get("upstreamDataTransferredInBytesInLast24Hours")),
                         c.get("topApplicationCategory"), c.get("detectedOs"),
                         ", ".join(f for f, on in (("watch", c.get("isWatchlisted")), ("blocked", c.get("isBlocked")))
                                   if on) or None])
        out.append(table(["", "Name", "IP", "MAC", "Network", "VLAN", "Connected to", "Signal", "Health",
                          "Connected for", "Down 24h", "Up 24h", "Top app", "OS", "Flags"], rows))
        out += ["", f"#### Offline / recently seen ({len(offline)})", ""]
        out.append(table(
            ["Name", "MAC", "Type", "Network", "Last seen on", "Offline since", "Watchlisted", "Alerts"],
            [[client_name(c), c.get("macAddress"), c.get("clientType"),
              client_network(c), c.get("deviceName"),
              clock.fmt(c.get("lastStateChange")), c.get("isWatchlisted"), len(c.get("activeAlerts") or [])]
             for c in sorted(offline, key=lambda c: -(c.get("lastStateChange") or 0))]
        ))

        # ---- application usage
        out += ["", "### Application usage", "", "Source: `applicationCategoryUsage` (last 24h)", ""]
        usage = _elements(eps.get("applicationCategoryUsage"))
        out.append(table(
            ["Category", "Network", "Down", "Up", "Blocked", "Applications"],
            [[_label(u.get("applicationCategory") or ""), u.get("networkSsid") or (u.get("wiredNetworkDescription") or {}).get("name"),
              human_bytes(u.get("downstreamDataTransferredDuringLast24HoursInBytes")),
              human_bytes(u.get("upstreamDataTransferredDuringLast24HoursInBytes")), u.get("isBlocked"),
              ", ".join(f"{a.get('applicationName')} ({human_bytes(a.get('dataTransferredInBytes'))})"
                        for a in u.get("applicationUsage") or [])]
             for u in sorted(usage, key=lambda u: -((u.get("downstreamDataTransferredDuringLast24HoursInBytes") or 0)
                                                    + (u.get("upstreamDataTransferredDuringLast24HoursInBytes") or 0)))]
        ))

        # ---- events
        events = _elements(eps.get("events"))
        out += ["", f"### Events ({len(events)})", "", "Source: `events`", "",
                "Counts: " + ", ".join(f"`{k}` × {v}" for k, v in Counter(e.get("event") for e in events).most_common()), ""]
        out.append(table(
            ["Time", "Event", "State", "Category", "Source", "Details", "Account"],
            [[clock.fmt(e.get("occurrenceTime")), e.get("event"), e.get("state"), e.get("category"),
              (e.get("source") or {}).get("name"),
              ", ".join(f"{a.get('key')}={a.get('valueStr') or a.get('valueEnum')}" for a in e.get("attributes") or []),
              (e.get("account") or {}).get("email") if isinstance(e.get("account"), dict) else e.get("account")]
             for e in sorted(events, key=lambda e: -(e.get("occurrenceTime") or 0))]
        ))

        # ---- administration
        admin = eps.get("administration") or {}
        out += ["", "### Administration", "", "Source: `administration`", ""]
        out.append(table(
            ["Email", "Role", "Activated", "MFA", "Locked", "Primary", "Current user"],
            [[a.get("email"), a.get("roleOnSite"), a.get("isActivated"), a.get("isMfaEnabled"), a.get("isLocked"),
              a.get("isPrimaryAccount"), a.get("isCurrentUser")] for a in admin.get("accounts") or []]
        ))
        out += ["", kv_table({k: v for k, v in admin.items()
                              if k not in ("accounts", "detectedLocation", "configuredLocation") and not isinstance(v, (dict, list))}), ""]

        # ---- maintenance / timezone
        maint = dict(eps.get("maintenance") or {})
        if maint.get("lastUpdateTime"):
            maint["lastUpdateTime"] = clock.fmt(maint["lastUpdateTime"])
        meta = maint.pop("metaData", None) or {}
        maint.update({f"metaData.{k}": v for k, v in meta.items() if k != "kind"})
        out += ["### Maintenance", "", "Source: `maintenance`, `timezone`", "", kv_table(maint), "",
                kv_table(eps.get("timezone") or {}), ""]

        # ---- anything not rendered above
        handled = {"inventory", "clientSummary", "alerts", "alertsSummary", "networksSummary", "wiredNetworks",
                   "applicationCategoryUsage", "administration", "timezone", "maintenance", "systemHealth",
                   "events", "landingPage"}
        out += ["### Other endpoints", ""]
        extra = {k: v for k, v in eps.items() if k not in handled}
        if not extra:
            out += ["_All collected endpoints are rendered above._", ""]
        for k, v in extra.items():
            out += [f"**{k}**", "", "```json", json.dumps(v, indent=2)[:20000], "```", ""]
        if data["errors"]:
            out += ["**Collection errors**", "", table(["Endpoint", "Error"], list(data["errors"].items()))]

    return "\n".join(out)
