"""Config flow for Stopfinder."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StopfinderApi, StopfinderAuthError, StopfinderError
from .const import (
    CONF_BASE_URI,
    CONF_CLIENT_KEYS,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_SF_CLIENT_ID,
    CONF_SUBSCRIBER_ID,
    CONF_USERNAME,
    DOMAIN,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class StopfinderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Stopfinder config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            # Generate a stable, app-independent device id for this entry so we
            # never collide with the user's phone in the server's device table.
            device_id = secrets.token_hex(8)  # 16 hex chars, like the Android ID

            session = async_get_clientsession(self.hass)
            api = StopfinderApi(session)
            try:
                await api.discover(username)
                tokens = await api.login(username, password, device_id)
                identity = await api.fetch_client_identity()
                subscriber = await api.fetch_subscriber()
            except StopfinderAuthError:
                errors["base"] = "invalid_auth"
            except StopfinderError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                subscriber_id = subscriber.get("id")
                await self.async_set_unique_id(str(subscriber_id))
                self._abort_if_unique_id_configured()

                primary = identity["primary"]
                title = f"Stopfinder ({api.client_keys or username})"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_DEVICE_ID: device_id,
                        CONF_BASE_URI: api.base_uri,
                        CONF_CLIENT_KEYS: api.client_keys,
                        CONF_SF_CLIENT_ID: api.sf_client_id,
                        CONF_SUBSCRIBER_ID: subscriber_id,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Trigger re-entry of the password if it stops working."""
        return await self.async_step_user()
