"""API client error mapping (mirrors the portal's SSO error handling)."""

from __future__ import annotations

import pytest

from custom_components.instant_on.api import (
    InstantOnAccountLockedError,
    InstantOnAuth,
    InstantOnAuthError,
    InstantOnConnectionError,
    InstantOnInvalidCredentialsError,
    InstantOnMfaRequiredError,
    InstantOnRateLimitedError,
    elements,
)


@pytest.mark.parametrize(
    ("status", "body", "otp", "expected", "message"),
    [
        (400, {"error": "invalid_grant", "error_description": "We didn't recognize the username or password"}, False, InstantOnInvalidCredentialsError, None),
        (400, {"error": "invalid_grant", "error_description": "Invalid OTP provided"}, False, InstantOnMfaRequiredError, "required"),
        (400, {"error": "invalid_grant", "error_description": "Invalid OTP provided"}, True, InstantOnMfaRequiredError, "Invalid"),
        (400, {"error": "invalid_credentials", "error_description": "Account is Locked"}, False, InstantOnAccountLockedError, None),
        (400, {"error": "invalid_grant", "error_description": "user disabled"}, False, InstantOnAccountLockedError, None),
        (400, {"error": "something_else"}, False, InstantOnAuthError, None),
        (429, {}, False, InstantOnRateLimitedError, None),
        (503, {}, False, InstantOnConnectionError, None),
    ],
)
def test_sso_error_mapping(status, body, otp, expected, message) -> None:
    with pytest.raises(expected) as err:
        InstantOnAuth._raise_sso_error(status, body, otp_supplied=otp)
    assert type(err.value) is expected
    if message:
        assert message in str(err.value)


def test_elements() -> None:
    assert elements({"elements": [1]}) == [1]
    assert elements([2]) == [2]
    assert elements(None) == []


def test_token_rotation_callback() -> None:
    seen: list[str] = []
    auth = InstantOnAuth(session=None, refresh_token="r1", on_refresh_token=seen.append)
    auth._store_tokens({"access_token": "a", "refresh_token": "r1", "expires_in": 10})
    auth._store_tokens({"access_token": "b", "refresh_token": "r2", "expires_in": 10})
    assert seen == ["r2"]
    assert auth.refresh_token == "r2"
    with pytest.raises(InstantOnAuthError):
        auth._store_tokens({"access_token": "c"})
