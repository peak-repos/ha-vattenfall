"""Sensor platform for Vattenfall."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_END_DATE,
    ATTR_HOURLY_END_DATE,
    ATTR_HOURLY_POINTS,
    ATTR_HOURLY_START_DATE,
    ATTR_POINTS,
    ATTR_START_DATE,
    DOMAIN,
)
from .coordinator import VattenfallDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class VattenfallSensorEntityDescription(SensorEntityDescription):
    """Describes Vattenfall sensor entity."""

    value_key: str
    attribute_group: str = "daily"  # "daily" | "hourly"


SENSORS: tuple[VattenfallSensorEntityDescription, ...] = (
    VattenfallSensorEntityDescription(
        key="finalized_day",
        translation_key="finalized_day",
        name="Last finalized day consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="finalized_day_kwh",
        icon="mdi:lightning-bolt",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="finalized_month_to_date",
        translation_key="finalized_month_to_date",
        name="Month-to-date consumption (finalized)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="finalized_month_to_date_kwh",
        icon="mdi:calendar-month",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="finalized_average_daily",
        translation_key="finalized_average_daily",
        name="Average daily consumption (finalized)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="finalized_average_daily_kwh",
        icon="mdi:chart-line",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="latest_hour",
        translation_key="latest_hour",
        name="Latest reported hour consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_hour_kwh",
        icon="mdi:clock-outline",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="today_partial",
        translation_key="today_partial",
        name="Today's consumption so far (partial)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_partial_kwh",
        icon="mdi:calendar-today",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="finalized_day_peak_hour",
        translation_key="finalized_day_peak_hour",
        name="Last finalized day peak hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="finalized_day_peak_hour_kwh",
        icon="mdi:chart-bell-curve-cumulative",
        attribute_group="hourly",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vattenfall sensors from config entry."""
    coordinator: VattenfallDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    entities = [
        VattenfallSensor(coordinator=coordinator, entry=entry, description=description)
        for description in SENSORS
    ]
    async_add_entities(entities)


class VattenfallSensor(CoordinatorEntity[VattenfallDataUpdateCoordinator], SensorEntity):
    """Representation of a Vattenfall sensor."""

    entity_description: VattenfallSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VattenfallDataUpdateCoordinator,
        entry: ConfigEntry,
        description: VattenfallSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Vattenfall",
            manufacturer="Vattenfall",
        )

    @property
    def native_value(self) -> float | None:
        """Return sensor value."""
        value: Any = self.coordinator.data.get(self.entity_description.value_key)
        if value is None:
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes scoped to this sensor's data group."""
        data = self.coordinator.data
        group = self.entity_description.attribute_group

        finalized_date = data.get("finalized_date")
        if group == "daily":
            return {
                "finalized_date": finalized_date,
                "start_date": data.get(ATTR_START_DATE),
                "end_date": data.get(ATTR_END_DATE),
                "points_count": len(data.get(ATTR_POINTS, [])),
            }
        if group == "hourly":
            return {
                "finalized_date": finalized_date,
                "start_date": data.get(ATTR_HOURLY_START_DATE),
                "end_date": data.get(ATTR_HOURLY_END_DATE),
                "points_count": len(data.get(ATTR_HOURLY_POINTS, [])),
                "peak_hour_time": data.get("finalized_day_peak_hour_time"),
            }
        return {}
