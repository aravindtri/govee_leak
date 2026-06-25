"""Binary sensors for the GoveeLife Water Leak integration."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_LOW_BATTERY_PCT, DOMAIN
from .entity import GoveeLeakEntity
from .runtime import GoveeLeakRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee leak binary sensors."""
    runtime: GoveeLeakRuntime = hass.data[DOMAIN][entry.entry_id]
    entities: list[GoveeLeakEntity] = []
    for device in runtime.states:
        entities.append(GoveeLeakBinarySensor(runtime, device))
        entities.append(GoveeLowBatteryBinarySensor(runtime, device))
    async_add_entities(entities)


class GoveeLeakBinarySensor(GoveeLeakEntity, BinarySensorEntity):
    """Water-leak (moisture) binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.MOISTURE
    _attr_name = None

    def __init__(self, runtime: GoveeLeakRuntime, device: str) -> None:
        super().__init__(runtime, device)
        self._attr_unique_id = f"{device}_leak"

    @property
    def is_on(self) -> bool | None:
        return self._state.leak


class GoveeLowBatteryBinarySensor(GoveeLeakEntity, BinarySensorEntity):
    """Low-battery diagnostic binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "low_battery"

    def __init__(self, runtime: GoveeLeakRuntime, device: str) -> None:
        super().__init__(runtime, device)
        self._attr_unique_id = f"{device}_low_battery"

    @property
    def is_on(self) -> bool | None:
        if self._state.battery is None:
            return None
        return self._state.battery <= DEFAULT_LOW_BATTERY_PCT
