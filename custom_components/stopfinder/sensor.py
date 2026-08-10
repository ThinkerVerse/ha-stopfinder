"""Sensors: GPS status and current bus number per student.

Separate entities (rather than only tracker attributes) so state changes get
recorded and can drive automations directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GPS_NO_SIGNAL, GPS_VALID
from .coordinator import RiderState, StopfinderCoordinator


@dataclass(frozen=True, kw_only=True)
class StopfinderSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RiderState], str | None]


def _gps_status(s: RiderState) -> str | None:
    """Derived status: the /gps payload has no status field, so infer it.

    ValidGPS when a fresh fix is present; NoSignal while a trip is active but no
    fresh fix has arrived; None (unknown) outside any trip window.
    """
    if s.active_trip is None:
        return None
    if s.fix and s.fix.is_fresh():
        return GPS_VALID
    return GPS_NO_SIGNAL


SENSORS: tuple[StopfinderSensorDescription, ...] = (
    StopfinderSensorDescription(
        key="gps_status",
        name="GPS status",
        icon="mdi:crosshairs-gps",
        value_fn=_gps_status,
    ),
    StopfinderSensorDescription(
        key="bus_number",
        name="Bus number",
        icon="mdi:bus",
        value_fn=lambda s: (s.active_trip.bus_number if s.active_trip else None),
    ),
    StopfinderSensorDescription(
        key="trip",
        name="Active trip",
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
