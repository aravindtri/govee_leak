"""AWS IoT MQTT client for Govee, run in a background thread.

paho-mqtt is blocking, so it lives on its own thread. Decoded readings are
handed back via a thread-safe callback (the runtime marshals onto the HA loop).
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import tempfile
import threading
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from .api import GoveeCreds
from .protocol import SensorReading, readings_from_message

_LOGGER = logging.getLogger(__name__)

ReadingsCallback = Callable[[str, list[SensorReading]], None]
ConnectCallback = Callable[[bool], None]


def _parse_payload(payload: bytes) -> dict | None:
    import json

    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


class GoveeIotClient:
    """Manages the mTLS MQTT connection to Govee's AWS IoT broker."""

    def __init__(
        self,
        creds: GoveeCreds,
        on_readings: ReadingsCallback,
        on_connection: ConnectCallback | None = None,
        poll_topics: list[str] | None = None,
    ) -> None:
        self._creds = creds
        self._on_readings = on_readings
        self._on_connection = on_connection
        self._poll_topics = poll_topics or []
        self._client: mqtt.Client | None = None
        self._cert_file: str | None = None
        self._key_file: str | None = None
        self._stop = threading.Event()
        self._connected = False
        # Monotonic timestamp of the last inbound message (or successful
        # connect). Used by the runtime watchdog to detect a silently dead
        # socket that paho hasn't noticed.
        self._last_activity = time.monotonic()

    def _write_certs(self) -> tuple[str, str]:
        cert = tempfile.NamedTemporaryFile(
            "wb", suffix=".pem", delete=False, prefix="govee_cert_"
        )
        cert.write(self._creds.cert_pem)
        cert.close()
        key = tempfile.NamedTemporaryFile(
            "wb", suffix=".pem", delete=False, prefix="govee_key_"
        )
        key.write(self._creds.key_pem)
        key.close()
        self._cert_file, self._key_file = cert.name, key.name
        return cert.name, key.name

    def _tls_context(self) -> ssl.SSLContext:
        cert_file, key_file = self._write_certs()
        ctx = ssl.create_default_context()
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def start(self) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=self._creds.mqtt_client_id,
        )
        client.tls_set_context(self._tls_context())
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        client.connect_async(self._creds.endpoint, 8883, keepalive=60)
        client.loop_start()

    def _on_connect(self, client, _userdata, _flags, rc) -> None:
        if rc == 0:
            self._connected = True
            self._last_activity = time.monotonic()
            client.subscribe(self._creds.topic, qos=0)
            _LOGGER.info("Connected to Govee IoT, subscribed to %s", self._creds.topic)
            self._poll_status(client)
            if self._on_connection:
                self._on_connection(True)
        else:
            self._connected = False
            _LOGGER.error("Govee IoT connect failed rc=%s", rc)
            if self._on_connection:
                self._on_connection(False)

    def _poll_status(self, client: mqtt.Client) -> None:
        """Ask each gateway for a full status dump so all sensors populate.

        On (re)connect the per-sensor states are otherwise unknown until each
        battery sensor next transmits. Publishing a 'status' poll to the
        gateway's command topic makes it broadcast a full dump on the account
        topic, which we already receive.
        """
        for topic in self._poll_topics:
            payload = json.dumps(
                {
                    "msg": {
                        "cmd": "status",
                        "cmdVersion": 0,
                        "transaction": f"v_{int(time.time() * 1000)}000",
                        "type": 0,
                    }
                }
            )
            try:
                client.publish(topic, payload, qos=0)
                _LOGGER.debug("Sent status poll to %s", topic)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Failed to publish status poll to %s", topic)

    def poll_now(self) -> bool:
        """Publish a status poll on demand (watchdog / manual refresh).

        Returns True if the poll was published, False if the client is not
        currently connected (paho will auto-reconnect and re-poll on connect).
        """
        client = self._client
        if client is None or not self._connected:
            return False
        self._poll_status(client)
        return True

    def last_activity_age(self) -> float:
        """Seconds since the last inbound message or successful connect."""
        return time.monotonic() - self._last_activity

    @property
    def connected(self) -> bool:
        return self._connected

    def _on_disconnect(self, _client, _userdata, rc) -> None:
        self._connected = False
        if not self._stop.is_set():
            _LOGGER.warning("Govee IoT disconnected rc=%s (will auto-reconnect)", rc)
            if self._on_connection:
                self._on_connection(False)

    def _on_message(self, _client, _userdata, msg) -> None:
        self._last_activity = time.monotonic()
        data = _parse_payload(msg.payload)
        if not data:
            return
        result = readings_from_message(data)
        if result is None:
            return
        gateway, readings = result
        try:
            self._on_readings(gateway, readings)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error dispatching Govee readings")

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        for path in (self._cert_file, self._key_file):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._cert_file = self._key_file = None
