"""Sensors: GPS status and current bus number per student.

Separate entities (rather than only tracker attributes) so state changes get
recorded and can drive automations directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GPS_STATUSES
from .coordinator import RiderState, StopfinderCoordinator


@dataclass(frozen=True, kw_only=True)
class StopfinderSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RiderState], str | None]


SENSORS: tuple[StopfinderSensorDescription, ...] = (
    StopfinderSensorDescription(
        key="gps_status",
        translation_key="gps_status",
        icon="mdi:crosshairs-gps",
        device_class=SensorDeviceClass.ENUM,
        options=GPS_STATUSES,
        value_fn=lambda s: s.gps_status(datetime.now(timezone.utc)),
    ),
    StopfinderSensorDescription(
        key="bus_number",
        translation_key="bus_number",
        icon="mdi:bus",
        value_fn=lambda s: (s.active_trip.bus_number if s.active_trip else None),
    ),
    StopfinderSensorDescription(
        key="trip",
        translation_key="trip",
        icon="mdi:map-marker-path",
        value_fn=lambda s: (s.active_trip.name if s.active_trip else None),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StopfinderCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for rider_id in (coordinator.data or {}):
        for desc in SENSORS:
            entities.append(StopfinderSensor(coordinator, rider_id, desc))
    async_add_entities(entities)


class StopfinderSensor(CoordinatorEntity[StopfinderCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: StopfinderSensorDescription

    def __init__(
        self,
        coordinator: StopfinderCoordinator,
        rider_id: int,
        description: StopfinderSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._rider_id = rider_id
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{rider_id}_{description.key}"

    @property
    def _state(self) -> RiderState | None:
        return (self.coordinator.data or {}).get(self._rider_id)

    @property
    def device_info(self) -> DeviceInfo:
        state = self._state
        name = "Stopfinder student"
        if state:
            name = f"{state.schedule.first_name} {state.schedule.last_name}".strip()
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._rider_id))},
            name=name or f"Rider {self._rider_id}",
            manufacturer="Transfinder",
            model="Stopfinder",
        )

    @property
    def native_value(self) -> str | None:
        state = self._state
        if not state:
            return None
        return self.entity_description.value_fn(state)
