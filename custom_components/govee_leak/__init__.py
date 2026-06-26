"""The GoveeLife Water Leak (H5059) integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import GoveeAuthError, NeedsVerificationCode
from .const import DOMAIN
from .runtime import GoveeLeakRuntime

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GoveeLife Water Leak from a config entry."""
    runtime = GoveeLeakRuntime(
        hass,
        entry,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
    )
    try:
        await runtime.async_setup()
    except NeedsVerificationCode as err:
        # Govee wants a fresh emailed code; the user must re-authenticate.
        raise ConfigEntryAuthFailed(
            "Govee requires a new email verification code"
        ) from err
    except GoveeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001
        # Network blips, Govee 5xx, cert decode, etc. -> let HA retry later.
        raise ConfigEntryNotReady(f"Could not connect to Govee: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: GoveeLeakRuntime = hass.data[DOMAIN].pop(entry.entry_id)
        await runtime.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
