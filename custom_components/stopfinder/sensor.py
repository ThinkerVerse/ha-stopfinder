"""Sensors: GPS status and current bus number per student.

Separate entities (rather than only tracker attributes) so state changes get
recorded and can drive automations directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SUBSCRIBER_ID, DOMAIN, GPS_STATUSES
from .api import Announcement
from .coordinator import RiderState, StopfinderCoordinator


@dataclass(frozen=True, kw_only=True)
class StopfinderSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RiderState], str | datetime | None]
    attrs_fn: Callable[[RiderState], dict[str, Any]] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stop_attrs(trip, pickup: bool) -> dict[str, Any]:
    """Which stop a scheduled time refers to."""
    if trip is None:
        return {}
    return {
        "stop": trip.pickup_stop_name if pickup else trip.dropoff_stop_name,
        "trip": trip.name,
        "bus_number": trip.bus_number,
        "to_school": trip.to_school,
    }


def _geo_alert_attrs(s: RiderState) -> dict[str, Any]:
    """Detail for the last geo alert, alongside its timestamp state."""
    alert = s.geo_alert
    if alert is None:
        return {}
    return {
        "zone": alert.zone_name,
        "subject": alert.subject,
        "message": alert.body,
        "trip_id": alert.trip_id,
        "alert_type": alert.alert_type,
        "alert_id": alert.alert_id,
    }


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
    StopfinderSensorDescription(
        key="geo_alert",
        translation_key="geo_alert",
        icon="mdi:map-marker-alert",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: (s.geo_alert.sent_on if s.geo_alert else None),
        attrs_fn=_geo_alert_attrs,
    ),
    # --- scheduled stop times, from pickUpTime/dropOffTime ------------------
    # These are the times the bus reaches *this student's* stop, as opposed to
    # start/finish which bracket the whole route.
    StopfinderSensorDescription(
        key="next_pickup",
        translation_key="next_pickup",
        icon="mdi:bus-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: (
            trip.adjusted_pickup_time
            if (trip := s.next_pickup(_now())) is not None
            else None
        ),
        attrs_fn=lambda s: _stop_attrs(s.next_pickup(_now()), pickup=True),
    ),
    StopfinderSensorDescription(
        key="next_dropoff",
        translation_key="next_dropoff",
        icon="mdi:bus-marker",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: (
            trip.adjusted_dropoff_time
            if (trip := s.next_dropoff(_now())) is not None
            else None
        ),
        attrs_fn=lambda s: _stop_attrs(s.next_dropoff(_now()), pickup=False),
    ),
    # --- profile: changes at most once or twice a school year ---------------
    StopfinderSensorDescription(
        key="student_name",
        translation_key="student_name",
        icon="mdi:account-school",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.schedule.full_name or None,
    ),
    StopfinderSensorDescription(
        key="grade",
        translation_key="grade",
        icon="mdi:school",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.schedule.grade or None,
    ),
    StopfinderSensorDescription(
        key="school",
        translation_key="school",
        icon="mdi:town-hall",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.schedule.school or None,
    ),
    StopfinderSensorDescription(
        key="district",
        translation_key="district",
        icon="mdi:map",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.district or None,
    ),
    StopfinderSensorDescription(
        key="home_stop",
        translation_key="home_stop",
        icon="mdi:sign-real-estate",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.schedule.home_stop or None,
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
    # Announcements are district-wide, so they hang off an account device rather
    # than being duplicated onto every student.
    for desc in ANNOUNCEMENT_SENSORS:
        entities.append(StopfinderAnnouncementSensor(coordinator, desc))
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
    def native_value(self) -> str | datetime | None:
        state = self._state
        if not state:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        attrs_fn = self.entity_description.attrs_fn
        if state is None or attrs_fn is None:
            return {}
        return attrs_fn(state)


# --------------------------------------------------------------------------
# announcements (account-wide, not per student)
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class StopfinderAnnouncementDescription(SensorEntityDescription):
    value_fn: Callable[[Announcement], str | datetime | None]
    attrs_fn: Callable[[Announcement], dict[str, Any]] | None = None


def _announcement_attrs(a: Announcement) -> dict[str, Any]:
    return {
        "message": a.body,
        "sent_on": a.sent_on.isoformat() if a.sent_on else None,
        "sent_by": a.sent_by_name,
        "read": a.read,
        "archived": a.archived,
        "announcement_id": a.announcement_id,
    }


ANNOUNCEMENT_SENSORS: tuple[StopfinderAnnouncementDescription, ...] = (
    StopfinderAnnouncementDescription(
        key="announcement",
        translation_key="announcement",
        icon="mdi:bullhorn",
        value_fn=lambda a: a.subject or a.name or None,
        attrs_fn=_announcement_attrs,
    ),
    # Separate from the text so automations can gate on freshness: this endpoint
    # keeps returning last year's notice, and its age is the only thing that
    # distinguishes a live one.
    StopfinderAnnouncementDescription(
        key="announcement_time",
        translation_key="announcement_time",
        icon="mdi:bullhorn-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda a: a.sent_on,
    ),
)


class StopfinderAnnouncementSensor(
    CoordinatorEntity[StopfinderCoordinator], SensorEntity
):
    """The most recent district announcement."""

    _attr_has_entity_name = True
    entity_description: StopfinderAnnouncementDescription

    def __init__(
        self,
        coordinator: StopfinderCoordinator,
        description: StopfinderAnnouncementDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        subscriber_id = coordinator.entry.data.get(CONF_SUBSCRIBER_ID, "account")
        self._account_id = str(subscriber_id)
        self._attr_unique_id = f"{DOMAIN}_account_{subscriber_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        district = self.coordinator.district
        return DeviceInfo(
            identifiers={(DOMAIN, f"account_{self._account_id}")},
            name=f"Stopfinder ({district})" if district else "Stopfinder",
            manufacturer="Transfinder",
            model="Stopfinder",
        )

    @property
    def native_value(self) -> str | datetime | None:
        announcement = self.coordinator.announcement
        if announcement is None:
            return None
        value = self.entity_description.value_fn(announcement)
        if isinstance(value, str):
            # Home Assistant rejects states longer than 255 characters.
            return value[:255]
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        announcement = self.coordinator.announcement
        attrs_fn = self.entity_description.attrs_fn
        if announcement is None or attrs_fn is None:
            return {}
        return attrs_fn(announcement)
