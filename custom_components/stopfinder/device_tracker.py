"""Device tracker: one GPS entity per student, following their assigned bus."""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RiderState, StopfinderCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StopfinderCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        StopfinderBusTracker(coordinator, rider_id)
        for rider_id in (coordinator.data or {})
    )


class StopfinderBusTracker(CoordinatorEntity[StopfinderCoordinator], TrackerEntity):
    """The bus currently carrying a given student."""

    _attr_has_entity_name = True
    _attr_name = "Bus"
    _attr_icon = "mdi:bus-school"

    def __init__(self, coordinator: StopfinderCoordinator, rider_id: int) -> None:
        super().__init__(coordinator)
        self._rider_id = rider_id
        # keyed on the STUDENT, not the bus number (substitutions change the number)
        self._attr_unique_id = f"{DOMAIN}_{rider_id}_bus"

    @property
    def _state(self) -> RiderState | None:
        return (self.coordinator.data or {}).get(self._rider_id)

    def _fresh_fix(self):
        state = self._state
        if state and state.fix and state.fix.is_fresh():
            return state.fix
        return None

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
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def available(self) -> bool:
        return self._fresh_fix() is not None

    @property
    def latitude(self) -> float | None:
        fix = self._fresh_fix()
        return fix.latitude if fix else None

    @property
    def longitude(self) -> float | None:
        fix = self._fresh_fix()
        return fix.longitude if fix else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        if not state:
            return {}
        attrs: dict[str, object] = {}
        trip = state.active_trip
        if trip:
            attrs.update(
                {
                    "bus_number": trip.bus_number,
                    "trip": trip.name,
                    "to_school": trip.to_school,
                    "pickup_stop": trip.pickup_stop_name,
                    "dropoff_stop": trip.dropoff_stop_name,
                }
            )
        if state.fix:
            attrs["reported_at"] = state.fix.fix_time.isoformat()
        return attrs
