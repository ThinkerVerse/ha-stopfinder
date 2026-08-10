"""The Stopfinder integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BASE_URI,
    CONF_CLIENT_KEYS,
    CONF_SF_CLIENT_ID,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import StopfinderCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Stopfinder from a config entry."""
    coordinator = StopfinderCoordinator(hass, entry)

    # Prime cached identity from the entry so the first calls have their headers.
    coordinator.api.base_uri = entry.data.get(CONF_BASE_URI)
    coordinator.api.client_keys = entry.data.get(CONF_CLIENT_KEYS, "")
    coordinator.api.sf_client_id = entry.data.get(CONF_SF_CLIENT_ID)

    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options in place.

    This deliberately does not reload the entry: the coordinator writes the
    rotated refresh token back into entry.data, which also fires this listener,
    and reloading there would restart the integration on every token renewal.
    """
    coordinator: StopfinderCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        coordinator.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: StopfinderCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()
    return unload_ok
