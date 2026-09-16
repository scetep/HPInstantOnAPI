#!/usr/bin/env python3
"""
Collector for the (undocumented) HPE Aruba Networking Instant On cloud API.

The API is reverse-engineered from the web portal (portal.instant-on.hpe.com)
and is NOT supported by HPE -- endpoints and fields can change without notice.

Auth flow (OAuth2 authorization-code + PKCE, same as the portal):
  1. GET  {portal}/settings.json                     -> SSO host, client id, redirect uri, API base
  2. POST {sso}/aio/api/v1/mfa/validate/full         -> session token (username/password form)
  3. GET  {sso}/as/authorization.oauth2?...          -> 302 with ?code=... (not followed)
  4. POST {sso}/as/token.oauth2                      -> bearer access_token

The account used must NOT have MFA enabled. Create a dedicated read-only
("viewer") account in the Instant On portal for this.

Credentials come from the environment (or a .env file next to this script):
  INSTANTON_USER, INSTANTON_PASS

Usage:
  ./instanton.py                 # collect everything, write JSON under ./output/<timestamp>/
  ./instanton.py --summary-only  # just print the health summary
  ./instanton.py --json          # print the summary as JSON (for monitoring tools)
  ./instanton.py --endpoint inventory --site <id>   # dump a single endpoint
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

PORTAL = "https://portal.instant-on.hpe.com"
API_VERSION = "7"
SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "instanton" / "token.json"

# Per-site endpoints known from the portal / community scripts.
# Some may 404 depending on API version or site type; those are recorded, not fatal.
SITE_ENDPOINTS = [
    "landingPage",
    "inventory",
    "clientSummary",
    "alerts",
    "alertsSummary",
    "networksSummary",
    "wiredNetworks",
    "applicationCategoryUsage",
    "administration",
    "timezone",
    "maintenance",
]


# --------------------------------------------------------------------------- auth

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


class InstantOnClient:
    def __init__(self, username: str, password: str, timeout: int = 30):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "instanton-monitor/0.1"
        self.settings = self._get_settings()
        self.api_base = self.settings["restApiUrl"].rstrip("/") + "/api"
        self._token: str | None = None

    def _get_settings(self) -> dict:
        r = self.http.get(f"{PORTAL}/settings.json", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # -- token handling -----------------------------------------------------

    def _read_cached_token(self) -> str | None:
        try:
            data = json.loads(TOKEN_CACHE.read_text())
        except (OSError, ValueError):
            return None
        if data.get("user") != self.username or data.get("expires_at", 0) < time.time() + 60:
            return None
        return data.get("access_token")

    def _write_cached_token(self, token: str, expires_in: int) -> None:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.touch(mode=0o600, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({
            "user": self.username,
            "access_token": token,
            "expires_at": time.time() + expires_in,
        }))

    def login(self, force: bool = False) -> str:
        if not force:
            cached = self._read_cached_token()
            if cached:
                self._token = cached
                return cached

        sso = self.settings["ssoFqdn"].rstrip("/")
        client_id = self.settings["ssoClientIdAuthZ"]
        redirect_uri = self.settings["ssoRedirectUrl"]

        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(24))

        # 1. username/password -> session token
        r = self.http.post(
            f"{sso}/aio/api/v1/mfa/validate/full",
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")
        session_token = r.json().get("access_token")
        if not session_token:
            raise RuntimeError(
                "Login response had no session token. Is MFA enabled on this account? "
                f"Response keys: {list(r.json())}"
            )

        # 2. authorize -> redirect carrying ?code=
        r = self.http.get(
            f"{sso}/as/authorization.oauth2",
            params={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "profile openid",
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "sessionToken": session_token,
            },
            allow_redirects=False,
            timeout=self.timeout,
        )
        location = r.headers.get("Location", "")
        query = parse_qs(urlparse(location).query)
        if "code" not in query:
            raise RuntimeError(f"Authorization step returned no code (HTTP {r.status_code}, Location={location[:200]!r})")
        if query.get("state", [state])[0] != state:
            raise RuntimeError("OAuth state mismatch")

        # 3. code -> bearer token
        r = self.http.post(
            f"{sso}/as/token.oauth2",
            data={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code": query["code"][0],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        tok = r.json()
        self._token = tok["access_token"]
        self._write_cached_token(self._token, int(tok.get("expires_in", 1800)))
        return self._token

    # -- API ----------------------------------------------------------------

    def get(self, path: str) -> requests.Response:
        if not self._token:
            self.login()
        url = f"{self.api_base}/{path.lstrip('/')}"
        for attempt in range(2):
            r = self.http.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "x-ion-api-version": API_VERSION,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            if r.status_code == 401 and attempt == 0:
                self.login(force=True)  # cached token expired/revoked
                continue
            if r.status_code == 429 and attempt == 0:
                time.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            return r
        return r

    def get_json(self, path: str):
        r = self.get(path)
        r.raise_for_status()
        return r.json() if r.content else None

    def sites(self) -> list[dict]:
        data = self.get_json("sites/")
        return _elements(data)


# --------------------------------------------------------------------------- helpers

def _elements(data) -> list:
    """Most list endpoints return {"elements": [...]}; tolerate bare lists too."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("elements", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def collect_site(client: InstantOnClient, site: dict, endpoints: list[str]) -> dict:
    site_id = site["id"]
    result = {"site": site, "endpoints": {}, "errors": {}}
    for ep in endpoints:
        try:
            r = client.get(f"sites/{site_id}/{ep}")
        except requests.RequestException as exc:
            result["errors"][ep] = str(exc)
            continue
        if r.ok:
            result["endpoints"][ep] = r.json() if r.content else None
        else:
            result["errors"][ep] = f"HTTP {r.status_code}"
    return result


def summarize_site(data: dict) -> dict:
    """Condense raw endpoint data into a monitoring-friendly summary.

    Field names are best-effort guesses from the portal's responses; anything
    missing is reported as None rather than failing.
    """
    site = data["site"]
    eps = data["endpoints"]

    devices = []
    for dev in _elements(eps.get("inventory")):
        devices.append({
            "name": _first(dev, "name", "hostname"),
            "model": _first(dev, "model", "deviceModel", "modelName"),
            "type": _first(dev, "deviceType", "type"),
            "status": _first(dev, "status", "operationalState", "state"),
            "ip": _first(dev, "ipAddress", "ip"),
            "mac": _first(dev, "macAddress", "mac"),
            "serial": _first(dev, "serialNumber", "serial"),
            "firmware": _first(dev, "osVersion", "firmwareVersion", "softwareVersion"),
            "uptime_s": _first(dev, "uptimeInSeconds", "uptime"),
        })

    online_words = {"up", "online", "connected", "ok"}
    offline = [d for d in devices if str(d["status"]).lower() not in online_words]

    clients = _elements(eps.get("clientSummary"))
    wireless = [c for c in clients if c.get("wirelessNetworkId")]
    alerts = _elements(eps.get("alerts"))
    active_alerts = [a for a in alerts if not _first(a, "resolvedTime", "clearedTime", "isResolved")]

    return {
        "site_id": site.get("id"),
        "site_name": site.get("name"),
        "site_health": _first(site, "health", "healthStatus", "status"),
        "devices_total": len(devices),
        "devices_not_up": len(offline),
        "devices": devices,
        "clients_total": len(clients),
        "clients_wireless": len(wireless),
        "clients_wired": len(clients) - len(wireless),
        "alerts_total": len(alerts),
        "alerts_active": len(active_alerts),
        "endpoint_errors": data["errors"],
    }


def print_summary(summaries: list[dict]) -> None:
    for s in summaries:
        print(f"\n=== {s['site_name']}  ({s['site_id']})  health={s['site_health']}")
        print(f"  Devices: {s['devices_total']} total, {s['devices_not_up']} not up")
        for d in s["devices"]:
            print(f"    - {str(d['name']):<24} {str(d['model']):<16} {str(d['status']):<10} "
                  f"{str(d['ip']):<16} fw={d['firmware']}")
        print(f"  Clients: {s['clients_total']} ({s['clients_wireless']} wireless, {s['clients_wired']} wired)")
        print(f"  Alerts:  {s['alerts_total']} total, {s['alerts_active']} active")
        if s["endpoint_errors"]:
            print(f"  Endpoint errors: {s['endpoint_errors']}")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Pull data from the Instant On cloud API")
    ap.add_argument("--site", help="only this site id")
    ap.add_argument("--endpoint", help="dump a single endpoint (e.g. inventory) as JSON")
    ap.add_argument("--output", default=str(SCRIPT_DIR / "output"), help="directory for raw JSON dumps")
    ap.add_argument("--summary-only", action="store_true", help="don't write raw JSON files")
    ap.add_argument("--json", action="store_true", help="print summary as JSON")
    args = ap.parse_args()

    _load_dotenv(SCRIPT_DIR / ".env")
    user, pw = os.environ.get("INSTANTON_USER"), os.environ.get("INSTANTON_PASS")
    if not user or not pw:
        print("Set INSTANTON_USER and INSTANTON_PASS (env or .env file).", file=sys.stderr)
        return 2

    client = InstantOnClient(user, pw)
    sites = client.sites()
    if args.site:
        sites = [s for s in sites if s.get("id") == args.site]
    if not sites:
        print("No sites found.", file=sys.stderr)
        return 1

    if args.endpoint:
        out = {s["id"]: client.get_json(f"sites/{s['id']}/{args.endpoint}") for s in sites}
        print(json.dumps(out, indent=2))
        return 0

    raw = [collect_site(client, s, SITE_ENDPOINTS) for s in sites]
    summaries = [summarize_site(d) for d in raw]

    if not args.summary_only:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = Path(args.output) / stamp
        for d in raw:
            sdir = outdir / d["site"]["id"]
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "site.json").write_text(json.dumps(d["site"], indent=2))
            for ep, payload in d["endpoints"].items():
                (sdir / f"{ep}.json").write_text(json.dumps(payload, indent=2))
        (outdir / "summary.json").write_text(json.dumps(summaries, indent=2))
        if not args.json:
            print(f"Raw data written to {outdir}")

    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        print_summary(summaries)

    # Non-zero exit when something looks wrong -> usable from cron / Nagios-style checks.
    unhealthy = any(s["devices_not_up"] or s["alerts_active"] for s in summaries)
    return 1 if unhealthy else 0


if __name__ == "__main__":
    sys.exit(main())
