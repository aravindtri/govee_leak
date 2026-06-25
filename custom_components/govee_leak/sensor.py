"""Sensors for the GoveeLife Water Leak integration."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import GoveeLeakEntity
from .runtime import GoveeLeakRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee leak battery sensors."""
    runtime: GoveeLeakRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GoveeBatterySensor(runtime, device) for device in runtime.states
    )


class GoveeBatterySensor(GoveeLeakEntity, SensorEntity):
    """Battery percentage sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "battery"

    def __init__(self, runtime: GoveeLeakRuntime, device: str) -> None:
        super().__init__(runtime, device)
        self._attr_unique_id = f"{device}_battery"

    @property
    def native_value(self) -> int | None:
        return self._state.battery
