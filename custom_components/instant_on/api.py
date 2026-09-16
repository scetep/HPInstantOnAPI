"""Async client for the (unofficial) HPE Aruba Networking Instant On cloud API.

The flow mirrors the web portal (portal.instant-on.hpe.com):

1. POST {sso}/aio/api/v1/mfa/validate/full   username, password[, otp] -> session token
   A missing/wrong one-time code is reported as ``invalid_grant`` with "OTP"
   in ``error_description``; the portal then asks for the code and re-posts.
2. GET  {sso}/as/authorization.oauth2 (PKCE)   -> 302 Location ...?code=
3. POST {sso}/as/token.oauth2 (authorization_code) -> access + refresh token
4. POST {sso}/as/token.oauth2 (refresh_token)      -> rotated access + refresh token

Only the refresh token is meant to be persisted; the password is never stored.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

PORTAL_URL = "https://portal.instant-on.hpe.com"
API_VERSION = "7"
TIMEOUT = aiohttp.ClientTimeout(total=30)
# Identify ourselves so HPE can tell this traffic apart from the portal.
try:
    _VERSION = json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]
except (OSError, ValueError, KeyError):
    _VERSION = "0"
USER_AGENT = f"ha-instant-on/{_VERSION} (+https://github.com/scetep/HPInstantOnAPI)"
FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "User-Agent": USER_AGENT,
}


class InstantOnError(Exception):
    """Base error."""


class InstantOnConnectionError(InstantOnError):
    """Network problem or unexpected server response."""


class InstantOnRateLimitedError(InstantOnConnectionError):
    """HTTP 429 from the API or SSO."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(headers: Any) -> float | None:
    """Seconds from a Retry-After header (only the delta-seconds form is used)."""
    try:
        value = float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class InstantOnAuthError(InstantOnError):
    """Credentials or tokens rejected; the user must sign in again."""


class InstantOnInvalidCredentialsError(InstantOnAuthError):
    """Wrong username or password."""


class InstantOnMfaRequiredError(InstantOnAuthError):
    """The account needs a one-time code (or the one supplied was wrong)."""


class InstantOnAccountLockedError(InstantOnAuthError):
    """Account locked or disabled."""


@dataclass
class Tokens:
    """OAuth tokens."""

    access_token: str
    refresh_token: str
    expires_at: float


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def elements(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("elements"), list):
        return data["elements"]
    return []


class InstantOnAuth:
    """Handles sign-in and token refresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._settings: dict[str, Any] | None = None
        self._tokens: Tokens | None = (
            Tokens("", refresh_token, 0) if refresh_token else None
        )
        self._on_refresh_token = on_refresh_token
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str | None:
        return self._tokens.refresh_token if self._tokens else None

    async def settings(self) -> dict[str, Any]:
        """Portal settings: SSO host, OAuth client id, redirect URI, API base."""
        if self._settings is None:
            data = await self._request_json("GET", f"{PORTAL_URL}/settings.json")
            for key in ("restApiUrl", "ssoFqdn", "ssoClientIdAuthZ", "ssoRedirectUrl"):
                if not data.get(key):
                    raise InstantOnConnectionError(f"settings.json is missing {key}")
            self._settings = data
        return self._settings

    async def api_base(self) -> str:
        return (await self.settings())["restApiUrl"].rstrip("/") + "/api"

    # -- sign-in ------------------------------------------------------------

    async def login(self, username: str, password: str, otp: str | None = None) -> Tokens:
        """Full sign-in. Raises InstantOnMfaRequiredError when a code is needed."""
        settings = await self.settings()
        sso = settings["ssoFqdn"].rstrip("/")
        client_id = settings["ssoClientIdAuthZ"]
        redirect_uri = settings["ssoRedirectUrl"]

        form = {"username": username, "password": password}
        if otp:
            form["otp"] = otp.strip().replace(" ", "")
        status, body, retry = await self._post_form(f"{sso}/aio/api/v1/mfa/validate/full", form)
        if status != 200 or body.get("error"):
            self._raise_sso_error(status, body, otp_supplied=bool(otp), retry_after=retry)
        session_token = body.get("access_token")
        if not session_token:
            raise InstantOnConnectionError("Sign-in response did not contain a session token")

        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(24))
        try:
            async with self._session.get(
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
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            ) as resp:
                location = resp.headers.get("Location", "")
                auth_status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise InstantOnConnectionError(str(err)) from err

        query = parse_qs(urlparse(location).query)
        if "code" not in query:
            raise InstantOnAuthError(
                f"Authorization step returned no code (HTTP {auth_status}, "
                f"error={query.get('error', ['?'])[0]})"
            )
        if query.get("state", [state])[0] != state:
            raise InstantOnAuthError("OAuth state mismatch")

        status, body, retry = await self._post_form(
            f"{sso}/as/token.oauth2",
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code": query["code"][0],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
            },
        )
        if status != 200:
            self._raise_sso_error(status, body, otp_supplied=bool(otp), retry_after=retry)
        return self._store_tokens(body)

    async def refresh(self) -> Tokens:
        """Exchange the refresh token for new tokens (the refresh token rotates)."""
        if not self._tokens or not self._tokens.refresh_token:
            raise InstantOnAuthError("No refresh token; sign in again")
        settings = await self.settings()
        status, body, retry = await self._post_form(
            f"{settings['ssoFqdn'].rstrip('/')}/as/token.oauth2",
            {
                "grant_type": "refresh_token",
                "client_id": settings["ssoClientIdAuthZ"],
                "refresh_token": self._tokens.refresh_token,
            },
        )
        if status == 429:
            raise InstantOnRateLimitedError("Rate limited while refreshing token", retry)
        if status >= 500:
            raise InstantOnConnectionError(f"Token refresh failed (HTTP {status})")
        if status != 200:
            raise InstantOnAuthError(
                f"Refresh token rejected: {body.get('error')} {body.get('error_description', '')}".strip()
            )
        return self._store_tokens(body)

    async def revoke(self) -> None:
        """Revoke the refresh token (used when the integration is removed)."""
        if not self.refresh_token:
            return
        settings = await self.settings()
        await self._post_form(
            f"{settings['ssoFqdn'].rstrip('/')}/as/revoke_token.oauth2",
            {"client_id": settings["ssoClientIdAuthZ"], "token": self.refresh_token},
        )

    async def access_token(self, force_refresh: bool = False) -> str:
        async with self._lock:
            if (
                force_refresh
                or not self._tokens
                or not self._tokens.access_token
                or self._tokens.expires_at < time.time() + 60
            ):
                await self.refresh()
            assert self._tokens
            return self._tokens.access_token

    def _store_tokens(self, body: dict[str, Any]) -> Tokens:
        if not body.get("access_token") or not body.get("refresh_token"):
            raise InstantOnAuthError("Token response is missing tokens")
        old = self.refresh_token
        self._tokens = Tokens(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=time.time() + int(body.get("expires_in", 1800)),
        )
        if self._on_refresh_token and body["refresh_token"] != old:
            self._on_refresh_token(body["refresh_token"])
        return self._tokens

    @staticmethod
    def _raise_sso_error(
        status: int, body: dict[str, Any], otp_supplied: bool, retry_after: float | None = None
    ) -> None:
        """Map SSO errors the same way the portal does."""
        if status == 429:
            raise InstantOnRateLimitedError("Too many sign-in attempts; try again later", retry_after)
        if status >= 500 or not body:
            raise InstantOnConnectionError(f"Sign-in failed (HTTP {status})")
        error = str(body.get("error", ""))
        description = str(body.get("error_description", ""))
        if error in ("invalid_grant", "invalid_credentials"):
            if "locked" in description.lower() or "disabled" in description.lower():
                raise InstantOnAccountLockedError(description)
            if "OTP" in description:
                raise InstantOnMfaRequiredError(
                    "Invalid verification code" if otp_supplied else "Verification code required"
                )
            raise InstantOnInvalidCredentialsError(description or "Invalid credentials")
        raise InstantOnAuthError(f"{error}: {description}".strip(": "))

    # -- HTTP ---------------------------------------------------------------

    async def _post_form(
        self, url: str, form: dict[str, str]
    ) -> tuple[int, dict[str, Any], float | None]:
        try:
            async with self._session.post(
                url, data=form, headers=FORM_HEADERS, timeout=TIMEOUT, allow_redirects=False
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = {}
                return resp.status, body if isinstance(body, dict) else {}, _retry_after(resp.headers)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise InstantOnConnectionError(str(err)) from err

    async def _request_json(self, method: str, url: str) -> Any:
        try:
            async with self._session.request(
                method, url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise InstantOnConnectionError(f"{url}: {err}") from err


class InstantOnClient:
    """Read-only API calls."""

    def __init__(self, session: aiohttp.ClientSession, auth: InstantOnAuth) -> None:
        self._session = session
        self.auth = auth

    async def get(self, path: str) -> Any:
        """GET /api/<path>, refreshing the access token once on 401."""
        url = f"{await self.auth.api_base()}/{path.lstrip('/')}"
        for attempt in range(2):
            token = await self.auth.access_token(force_refresh=attempt > 0)
            try:
                async with self._session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "x-ion-api-version": API_VERSION,
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=TIMEOUT,
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        continue
                    if resp.status == 401:
                        raise InstantOnAuthError("API rejected a freshly refreshed token")
                    if resp.status == 429:
                        raise InstantOnRateLimitedError(f"Rate limited on {path}", _retry_after(resp.headers))
                    if resp.status == 404:
                        return None
                    if resp.status >= 400:
                        raise InstantOnConnectionError(f"GET {path} failed (HTTP {resp.status})")
                    if resp.content_length == 0:
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise InstantOnConnectionError(f"GET {path}: {err}") from err
        return None

    async def sites(self) -> list[dict[str, Any]]:
        return elements(await self.get("sites/"))

    async def site_endpoints(self, site_id: str, endpoints: list[str]) -> dict[str, Any]:
        results = await asyncio.gather(*(self.get(f"sites/{site_id}/{ep}") for ep in endpoints))
        return dict(zip(endpoints, results, strict=True))
