"""Test bootstrap.

The integration lives at custom_components/stopfinder/, whose package __init__
imports Home Assistant. To test the parsing and window logic without a full HA
install, we:

  * expose the component directory as a synthetic package `sf`, so relative
    imports inside the modules resolve without running that __init__; and
  * register minimal stand-ins for the handful of HA symbols the coordinator
    touches, unless a real Home Assistant is importable.

Nothing here stubs the integration's own code — only its surroundings.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "stopfinder"


def _install_component_package() -> None:
    """Expose the component directory as package `sf` without running __init__."""
    if "sf" in sys.modules:
        return
    pkg = types.ModuleType("sf")
    pkg.__path__ = [str(COMPONENT_DIR)]
    sys.modules["sf"] = pkg


def _module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _install_homeassistant_stubs() -> None:
    """Register just enough of Home Assistant for the modules to import."""
    try:  # pragma: no cover - exercised only where real HA is installed
        import homeassistant  # noqa: F401

        return
    except ImportError:
        pass

    ha = _module("homeassistant")
    ha.__path__ = []

    config_entries = _module("homeassistant.config_entries")

    class ConfigEntry:
        def __init__(self, data: dict | None = None) -> None:
            self.data = data or {}
            self.entry_id = "test"

    class ConfigFlowResult(dict):
        pass

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs) -> None:
            super().__init_subclass__()

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlowResult = ConfigFlowResult
    config_entries.ConfigFlow = ConfigFlow

    core = _module("homeassistant.core")

    class HomeAssistant:
        pass

    core.HomeAssistant = HomeAssistant

    exceptions = _module("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryAuthFailed(HomeAssistantError):
        pass

    exceptions.HomeAssistantError = HomeAssistantError
    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed

    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []

    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None

    event = _module("homeassistant.helpers.event")
    event.async_track_time_interval = lambda hass, action, interval: (lambda: None)

    update_coordinator = _module("homeassistant.helpers.update_coordinator")
    T = TypeVar("T")

    class DataUpdateCoordinator(Generic[T]):
        def __init__(
            self,
            hass,
            logger,
            *,
            name: str | None = None,
            update_interval: timedelta | None = None,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data: T | None = None
            self.listener_updates = 0

        def async_set_updated_data(self, data) -> None:
            self.data = data

        def async_update_listeners(self) -> None:
            self.listener_updates += 1

    class UpdateFailed(Exception):
        pass

    class CoordinatorEntity(Generic[T]):
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    update_coordinator.CoordinatorEntity = CoordinatorEntity

    # --- entity-platform surface, for the import smoke test ------------------
    device_registry = _module("homeassistant.helpers.device_registry")

    class DeviceInfo(dict):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)

    device_registry.DeviceInfo = DeviceInfo

    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    components = _module("homeassistant.components")
    components.__path__ = []

    sensor = _module("homeassistant.components.sensor")

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        translation_key: str | None = None
        name: str | None = None
        icon: str | None = None
        device_class: object | None = None
        options: list[str] | None = None

    class SensorDeviceClass(Enum):
        ENUM = "enum"

    class SensorEntity:
        pass

    sensor.SensorEntityDescription = SensorEntityDescription
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorEntity = SensorEntity

    device_tracker = _module("homeassistant.components.device_tracker")

    class SourceType(Enum):
        GPS = "gps"

    class TrackerEntity:
        pass

    device_tracker.SourceType = SourceType
    device_tracker.TrackerEntity = TrackerEntity

    if "voluptuous" not in sys.modules:
        try:  # pragma: no cover - real voluptuous is fine too
            import voluptuous  # noqa: F401
        except ImportError:
            vol = _module("voluptuous")
            vol.Schema = lambda *a, **k: object()
            vol.Required = lambda *a, **k: object()


_install_homeassistant_stubs()
_install_component_package()
