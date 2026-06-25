"""Config flow for the GoveeLife Water Leak integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import GoveeAuthError, GoveeCloud, NeedsVerificationCode
from .const import CONF_CODE, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)
STEP_CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


class GoveeLeakConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the GoveeLife Water Leak config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._cloud: GoveeCloud | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            self._cloud = GoveeCloud(self._email, self._password)
            try:
                await self.hass.async_add_executor_job(self._cloud.login, None)
            except NeedsVerificationCode:
                try:
                    await self.hass.async_add_executor_job(
                        self._cloud.request_verification_code
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Failed to request Govee verification code")
                    errors["base"] = "code_request_failed"
                else:
                    return await self.async_step_code()
            except GoveeAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Govee login")
                errors["base"] = "cannot_connect"
            else:
                return self._create_entry()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._cloud is not None
        if user_input is not None:
            code = user_input[CONF_CODE].strip()
            try:
                await self.hass.async_add_executor_job(self._cloud.login, code)
            except GoveeAuthError:
                errors["base"] = "invalid_code"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error verifying Govee code")
                errors["base"] = "cannot_connect"
            else:
                return self._create_entry()

        return self.async_show_form(
            step_id="code", data_schema=STEP_CODE_SCHEMA, errors=errors
        )

    def _create_entry(self) -> ConfigFlowResult:
        assert self._email is not None and self._password is not None
        return self.async_create_entry(
            title=self._email,
            data={CONF_EMAIL: self._email, CONF_PASSWORD: self._password},
        )
