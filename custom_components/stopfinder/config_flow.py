"""Config flow for Stopfinder."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StopfinderApi, StopfinderAuthError, StopfinderError, Tokens
from .const import (
    ANNOUNCEMENT_POLL_MINUTES,
    ANNOUNCEMENT_POLL_MINUTES_MAX,
    ANNOUNCEMENT_POLL_MINUTES_MIN,
    CONF_ANNOUNCEMENT_POLL_MINUTES,
    CONF_BASE_URI,
    CONF_CLIENT_KEYS,
    CONF_DEVICE_ID,
    CONF_GPS_POLL_SECONDS,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SF_CLIENT_ID,
    CONF_SUBSCRIBER_ID,
    CONF_USERNAME,
    DOMAIN,
    GPS_POLL_SECONDS,
    GPS_POLL_SECONDS_MAX,
    GPS_POLL_SECONDS_MIN,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class StopfinderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Stopfinder config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> StopfinderOptionsFlow:
        return StopfinderOptionsFlow(config_entry)

    async def _async_validate(
        self, username: str, password: str, device_id: str
    ) -> tuple[StopfinderApi, Tokens, dict[str, Any]]:
        """Run the app's bootstrap sequence to prove the credentials work."""
        api = StopfinderApi(async_get_clientsession(self.hass))
        await api.discover(username)
        tokens = await api.login(username, password, device_id)
        await api.fetch_client_identity()
        subscriber = await api.fetch_subscriber()
        return api, tokens, subscriber

    def _entry_data(
        self,
        username: str,
        password: str,
        device_id: str,
        api: StopfinderApi,
        tokens: Tokens,
        subscriber_id: Any,
    ) -> dict[str, Any]:
        return {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            CONF_DEVICE_ID: device_id,
            CONF_BASE_URI: api.base_uri,
            CONF_CLIENT_KEYS: api.client_keys,
            CONF_SF_CLIENT_ID: api.sf_client_id,
            CONF_SUBSCRIBER_ID: subscriber_id,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
        }

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

            try:
                api, tokens, subscriber = await self._async_validate(
                    username, password, device_id
                )
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

                return self.async_create_entry(
                    title=f"Stopfinder ({api.client_keys or username})",
                    data=self._entry_data(
                        username, password, device_id, api, tokens, subscriber_id
                    ),
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication for an existing entry."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter the password for an entry that stopped authenticating.

        The email and device id are kept: re-using the entry's own device id
        avoids churning the server's device registration (which the app keys
        push re-registration on).
        """
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        errors: dict[str, str] = {}
        username = entry.data[CONF_USERNAME]
        device_id = entry.data[CONF_DEVICE_ID]

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            try:
                api, tokens, subscriber = await self._async_validate(
                    username, password, device_id
                )
            except StopfinderAuthError:
                errors["base"] = "invalid_auth"
            except StopfinderError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data=self._entry_data(
                        username,
                        password,
                        device_id,
                        api,
                        tokens,
                        subscriber.get("id", entry.data.get(CONF_SUBSCRIBER_ID)),
                    ),
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )


class StopfinderOptionsFlow(config_entries.OptionsFlow):
    """Let the poll cadence be tuned after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Keep the id rather than the entry: assigning self.config_entry is
        # deprecated in newer cores, and the base-class property that replaces it
        # does not exist in older ones.
        self._entry_id = config_entry.entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        options = entry.options if entry else {}
        gps_current = options.get(CONF_GPS_POLL_SECONDS, GPS_POLL_SECONDS)
        announcement_current = options.get(
            CONF_ANNOUNCEMENT_POLL_MINUTES, ANNOUNCEMENT_POLL_MINUTES
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GPS_POLL_SECONDS, default=gps_current
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=GPS_POLL_SECONDS_MIN, max=GPS_POLL_SECONDS_MAX),
                    ),
                    vol.Required(
                        CONF_ANNOUNCEMENT_POLL_MINUTES, default=announcement_current
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=ANNOUNCEMENT_POLL_MINUTES_MIN,
                            max=ANNOUNCEMENT_POLL_MINUTES_MAX,
                        ),
                    ),
                }
            ),
        )
