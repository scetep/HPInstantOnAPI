"""Config flow: username/password, optional MFA code, site selection, reauth, options."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    InstantOnAccountLockedError,
    InstantOnAuth,
    InstantOnClient,
    InstantOnConnectionError,
    InstantOnError,
    InstantOnInvalidCredentialsError,
    InstantOnMfaRequiredError,
    InstantOnRateLimitedError,
)
from .const import (
    CONF_OTP,
    CONF_PORT_SENSORS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SITES,
    CONF_TRACK_CLIENTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    TRACK_ALL,
    TRACK_NONE,
    TRACK_WATCHLIST,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        )
    }
)
OTP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OTP): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        )
    }
)


def _options_schema(sites: list[dict[str, Any]], current: Mapping[str, Any]) -> vol.Schema:
    schema: dict[Any, Any] = {}
    if len(sites) > 1:
        schema[vol.Optional(CONF_SITES, default=current.get(CONF_SITES, []))] = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=s["id"], label=s.get("name") or s["id"]) for s in sites],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
    schema[vol.Required(CONF_TRACK_CLIENTS, default=current.get(CONF_TRACK_CLIENTS, TRACK_ALL))] = SelectSelector(
        SelectSelectorConfig(
            options=[TRACK_ALL, TRACK_WATCHLIST, TRACK_NONE],
            translation_key=CONF_TRACK_CLIENTS,
            mode=SelectSelectorMode.LIST,
        )
    )
    schema[vol.Required(CONF_PORT_SENSORS, default=current.get(CONF_PORT_SENSORS, False))] = BooleanSelector()
    schema[
        vol.Required(CONF_SCAN_INTERVAL, default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    ] = vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL, max=900, step=5, unit_of_measurement="s", mode=NumberSelectorMode.BOX
            )
        ),
        vol.Coerce(int),
    )
    return vol.Schema(schema)


class InstantOnConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Instant On."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._refresh_token: str | None = None
        self._sites: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> InstantOnOptionsFlow:
        return InstantOnOptionsFlow()

    async def _try_login(self, otp: str | None, errors: dict[str, str]) -> bool | None:
        """True = signed in, None = code needed, False = error set."""
        session = async_create_clientsession(self.hass)
        auth = InstantOnAuth(session)
        try:
            tokens = await auth.login(self._username, self._password, otp)
            self._sites = await InstantOnClient(session, auth).sites()
        except InstantOnMfaRequiredError:
            if otp:
                errors["base"] = "invalid_otp"
                return False
            return None
        except InstantOnInvalidCredentialsError:
            errors["base"] = "invalid_auth"
        except InstantOnAccountLockedError:
            errors["base"] = "account_locked"
        except InstantOnRateLimitedError:
            errors["base"] = "rate_limited"
        except InstantOnConnectionError:
            errors["base"] = "cannot_connect"
        except InstantOnError:
            _LOGGER.exception("Unexpected Instant On sign-in error")
            errors["base"] = "unknown"
        else:
            # The rotated token after the sites call is the one to keep.
            self._refresh_token = auth.refresh_token or tokens.refresh_token
            return True
        return False

    async def _after_login(self) -> ConfigFlowResult:
        if self.source == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_REFRESH_TOKEN: self._refresh_token},
            )
        if not self._sites:
            # Nothing to set up; don't leave the new session open.
            auth = InstantOnAuth(async_create_clientsession(self.hass), self._refresh_token)
            with contextlib.suppress(InstantOnError):
                await auth.revoke()
            return self.async_abort(
                reason="no_sites", description_placeholders={"username": self._username or ""}
            )
        return await self.async_step_options()

    # -- initial setup --------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(self._username.lower())
            self._abort_if_unique_id_configured()
            result = await self._try_login(None, errors)
            if result is None:
                return await self.async_step_otp()
            if result:
                return await self._after_login()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_USERNAME: user_input[CONF_USERNAME]} if user_input else None
            ),
            errors=errors,
        )

    async def async_step_otp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._try_login(user_input[CONF_OTP], errors)
            if result:
                return await self._after_login()
            if result is None:  # should not happen with a code supplied
                errors["base"] = "invalid_otp"
        return self.async_show_form(
            step_id="otp",
            data_schema=OTP_SCHEMA,
            errors=errors,
            description_placeholders={"username": self._username or ""},
        )

    async def async_step_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._password = None
            title = self._username
            chosen = user_input.get(CONF_SITES) or [s["id"] for s in self._sites]
            if len(chosen) == 1:
                title = next(s.get("name") for s in self._sites if s["id"] == chosen[0])
            return self.async_create_entry(
                title=title,
                data={CONF_USERNAME: self._username, CONF_REFRESH_TOKEN: self._refresh_token},
                options=user_input,
            )
        return self.async_show_form(
            step_id="options",
            data_schema=_options_schema(self._sites, {}),
            description_placeholders={"sites": ", ".join(s.get("name", s["id"]) for s in self._sites)},
        )

    # -- reauth ---------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            result = await self._try_login(None, errors)
            if result is None:
                return await self.async_step_otp()
            if result:
                return await self._after_login()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=PASSWORD_SCHEMA,
            errors=errors,
            description_placeholders={"username": self._username or ""},
        )


class InstantOnOptionsFlow(OptionsFlowWithReload):
    """Change sites, client tracking, port sensors and polling."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        entry = self.config_entry
        coordinator = entry.runtime_data.coordinator
        try:
            sites = await coordinator.client.sites()
        except InstantOnError:
            sites = [data.site for data in (coordinator.data or {}).values()]
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(sites, entry.options)
        )
