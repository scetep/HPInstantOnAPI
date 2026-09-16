"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.instant_on.api import (
    InstantOnConnectionError,
    InstantOnInvalidCredentialsError,
    InstantOnMfaRequiredError,
    Tokens,
)
from custom_components.instant_on.const import (
    CONF_PORT_SENSORS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TRACK_CLIENTS,
    DOMAIN,
    TRACK_WATCHLIST,
)

from .conftest import USERNAME

OPTIONS = {CONF_TRACK_CLIENTS: TRACK_WATCHLIST, CONF_PORT_SENSORS: True, CONF_SCAN_INTERVAL: 120}


def _login_sequence(*results):
    """Patch InstantOnAuth.login to raise/return the given results in order."""
    results = list(results)

    async def login(self, username, password, otp=None):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        self._tokens = result
        return result

    return patch(
        "custom_components.instant_on.api.InstantOnAuth.login", autospec=True, side_effect=login
    )


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_without_mfa(hass: HomeAssistant, fake_api, mock_login) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USERNAME, "password": "secret"}
    )
    assert result["step_id"] == "options"
    with patch("custom_components.instant_on.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], OPTIONS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Site"
    assert result["data"] == {"username": USERNAME, CONF_REFRESH_TOKEN: "refresh-from-login"}
    assert "password" not in str(result["data"])
    assert result["options"] == OPTIONS
    assert mock_login.call_args.args[1:] == (USERNAME, "secret", None)


async def test_user_flow_with_mfa(hass: HomeAssistant, fake_api) -> None:
    tokens = Tokens("access", "refresh-after-otp", 9e9)
    with _login_sequence(
        InstantOnMfaRequiredError("code required"),
        InstantOnMfaRequiredError("wrong code"),
        tokens,
    ) as login:
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": "secret"}
        )
        assert result["step_id"] == "otp"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"otp": "000000"})
        assert result["step_id"] == "otp"
        assert result["errors"] == {"base": "invalid_otp"}

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"otp": "123 456"})
        assert result["step_id"] == "options"
    assert login.call_args.args[1:] == (USERNAME, "secret", "123 456")

    with patch("custom_components.instant_on.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], OPTIONS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-after-otp"


async def test_user_flow_errors(hass: HomeAssistant, fake_api) -> None:
    with _login_sequence(
        InstantOnInvalidCredentialsError("nope"), InstantOnConnectionError("down")
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": "bad"}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": USERNAME, "password": "bad"}
        )
        assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_account_aborts(hass: HomeAssistant, fake_api, mock_login, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USERNAME.upper(), "password": "secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_with_mfa(hass: HomeAssistant, fake_api, config_entry) -> None:
    config_entry.add_to_hass(hass)
    with patch("custom_components.instant_on.async_setup_entry", return_value=True):
        result = await config_entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        with _login_sequence(
            InstantOnMfaRequiredError("code required"), Tokens("a", "refresh-reauth", 9e9)
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"password": "secret"}
            )
            assert result["step_id"] == "otp"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"otp": "123456"}
            )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_REFRESH_TOKEN] == "refresh-reauth"


async def test_options_flow(hass: HomeAssistant, fake_api, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], OPTIONS)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == OPTIONS
    # Reloaded with port sensors enabled.
    assert hass.states.get("sensor.switch_2_port_1_speed") is not None
