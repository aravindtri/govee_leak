"""Runtime state holder for the GoveeLife Water Leak integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .api import GoveeCloud, GoveeCreds
from .const import SIGNAL_AVAILABILITY, SIGNAL_UPDATE
from .iot import GoveeIotClient
from .protocol import LeakSensor, SensorReading, parse_leak_sensors

_LOGGER = logging.getLogger(__name__)

# Govee's IoT cert/token are refreshed periodically by the app; re-auth daily
# so a stale cert can't silently kill the stream.
REAUTH_INTERVAL = timedelta(hours=12)


@dataclass
class SensorState:
    """Latest known state for one leak sensor."""

    sensor: LeakSensor
    leak: bool | None = None
    battery: int | None = None
    available: bool = False


@dataclass
class _SensorData:
    sensors: list[LeakSensor] = field(default_factory=list)


class GoveeLeakRuntime:
    """Owns the cloud auth, device list, and live IoT stream for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        email: str,
        password: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._cloud = GoveeCloud(email, password)
        self._iot: GoveeIotClient | None = None
        self._creds: GoveeCreds | None = None
        self._cancel_reauth = None
        # device id -> SensorState
        self.states: dict[str, SensorState] = {}
        # (gateway_device, sno) -> device id
        self._route: dict[tuple[str, int], str] = {}

    @property
    def sensors(self) -> list[LeakSensor]:
        return [s.sensor for s in self.states.values()]

    async def async_setup(self) -> None:
        """Authenticate, load devices, and start the IoT stream."""
        creds = await self.hass.async_add_executor_job(self._cloud.authenticate, None)
        self._creds = creds
        device_list = await self.hass.async_add_executor_job(
            self._cloud.device_list, creds.token
        )
        for sensor in parse_leak_sensors(device_list):
            self.states[sensor.device] = SensorState(
                sensor=sensor, battery=sensor.battery
            )
            self._route[(sensor.gateway_device, sensor.sno)] = sensor.device

        _LOGGER.info("Discovered %d Govee leak sensors", len(self.states))
        self._start_iot(creds)
        self._cancel_reauth = async_track_time_interval(
            self.hass, self._async_reauth, REAUTH_INTERVAL
        )

    def _start_iot(self, creds: GoveeCreds) -> None:
        self._iot = GoveeIotClient(
            creds,
            on_readings=self._handle_readings_threadsafe,
            on_connection=self._handle_connection_threadsafe,
        )
        self._iot.start()

    async def _async_reauth(self, _now) -> None:
        """Refresh credentials and restart the IoT connection."""
        try:
            creds = await self.hass.async_add_executor_job(
                self._cloud.authenticate, None
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Govee periodic re-auth failed: %s", err)
            return
        self._creds = creds
        if self._iot is not None:
            await self.hass.async_add_executor_job(self._iot.stop)
        self._start_iot(creds)
        _LOGGER.debug("Govee credentials refreshed")

    # -- threadsafe bridges (called from the paho thread) ----------------- #
    def _handle_readings_threadsafe(
        self, gateway: str, readings: list[SensorReading]
    ) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_readings, gateway, readings)

    def _handle_connection_threadsafe(self, connected: bool) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_connection, connected)

    @callback
    def _apply_readings(self, gateway: str, readings: list[SensorReading]) -> None:
        for reading in readings:
            device = self._route.get((gateway, reading.sno))
            if device is None:
                _LOGGER.debug(
                    "Reading for unknown sensor gw=%s sno=%s", gateway, reading.sno
                )
                continue
            state = self.states[device]
            if reading.leak is not None:
                state.leak = reading.leak
            if reading.battery is not None:
                state.battery = reading.battery
            state.available = True
            async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{device}")

    @callback
    def _apply_connection(self, connected: bool) -> None:
        for device, state in self.states.items():
            state.available = connected
            async_dispatcher_send(self.hass, f"{SIGNAL_AVAILABILITY}_{device}")

    async def async_shutdown(self) -> None:
        if self._cancel_reauth is not None:
            self._cancel_reauth()
            self._cancel_reauth = None
        if self._iot is not None:
            await self.hass.async_add_executor_job(self._iot.stop)
            self._iot = None
