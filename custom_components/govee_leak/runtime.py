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

# Watchdog: how often to check the stream and send a liveness poll.
WATCHDOG_INTERVAL = timedelta(minutes=5)
# If no inbound traffic for this long (even our own polls go unanswered) the
# socket is considered dead and we force a full reconnect.
STALE_AFTER = timedelta(minutes=15)


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
        self._cancel_watchdog = None
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
        await self._async_start_iot(creds)
        self._cancel_reauth = async_track_time_interval(
            self.hass, self._async_reauth, REAUTH_INTERVAL
        )
        self._cancel_watchdog = async_track_time_interval(
            self.hass, self._async_watchdog, WATCHDOG_INTERVAL
        )

    async def _async_start_iot(self, creds: GoveeCreds) -> None:
        poll_topics = sorted(
            {
                s.sensor.gateway_topic
                for s in self.states.values()
                if s.sensor.gateway_topic
            }
        )
        self._iot = GoveeIotClient(
            creds,
            on_readings=self._handle_readings_threadsafe,
            on_connection=self._handle_connection_threadsafe,
            poll_topics=poll_topics,
        )
        # Building the SSL context (load_default_certs / load_cert_chain) is
        # blocking, so start the client in the executor.
        await self.hass.async_add_executor_job(self._iot.start)

    async def _async_reauth(self, _now=None) -> None:
        """Refresh credentials and restart the IoT connection."""
        await self._async_restart_iot(reauth=True)

    async def _async_restart_iot(self, *, reauth: bool) -> None:
        """Tear down and restart the IoT stream, optionally re-authenticating."""
        if reauth or self._creds is None:
            try:
                creds = await self.hass.async_add_executor_job(
                    self._cloud.authenticate, None
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Govee re-auth failed: %s", err)
                self._mark_all_unavailable()
                return
            self._creds = creds
        if self._iot is not None:
            await self.hass.async_add_executor_job(self._iot.stop)
            self._iot = None
        try:
            await self._async_start_iot(self._creds)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Govee IoT restart failed: %s", err)
            self._mark_all_unavailable()
            return
        _LOGGER.debug("Govee IoT stream restarted (reauth=%s)", reauth)

    async def _async_watchdog(self, _now) -> None:
        """Detect a silently dead stream and recover it.

        paho auto-reconnects on a clean disconnect, but a half-open socket can
        stall without notice. Each tick we send a liveness poll; if nothing has
        come back for STALE_AFTER we force a full reconnect (with re-auth, in
        case the cert/token expired).
        """
        iot = self._iot
        if iot is None:
            return
        age = iot.last_activity_age()
        if age > STALE_AFTER.total_seconds():
            _LOGGER.warning(
                "No Govee IoT traffic for %.0fs; forcing reconnect", age
            )
            await self._async_restart_iot(reauth=True)
            return
        # Stream looks alive; nudge the gateway so states stay fresh.
        await self.hass.async_add_executor_job(iot.poll_now)

    async def async_poll_now(self) -> None:
        """Manually request a fresh status dump from the gateway(s)."""
        iot = self._iot
        if iot is None:
            return
        published = await self.hass.async_add_executor_job(iot.poll_now)
        if not published:
            _LOGGER.debug("Refresh requested while disconnected; reconnecting")
            await self._async_restart_iot(reauth=False)

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

    def _mark_all_unavailable(self) -> None:
        """Flag every sensor unavailable (called from the HA loop)."""
        self._handle_connection_threadsafe(False)

    async def async_shutdown(self) -> None:
        if self._cancel_reauth is not None:
            self._cancel_reauth()
            self._cancel_reauth = None
        if self._cancel_watchdog is not None:
            self._cancel_watchdog()
            self._cancel_watchdog = None
        if self._iot is not None:
            await self.hass.async_add_executor_job(self._iot.stop)
            self._iot = None
