"""Button platform for the GoveeLife Water Leak integration."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import GoveeLeakRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Govee leak refresh button."""
    runtime: GoveeLeakRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GoveeRefreshButton(runtime, entry.entry_id)])


class GoveeRefreshButton(ButtonEntity):
    """Polls the gateway(s) for a fresh status dump of every sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "refresh"

    def __init__(self, runtime: GoveeLeakRuntime, entry_id: str) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry_id}_refresh"

    async def async_press(self) -> None:
        await self._runtime.async_poll_now()
