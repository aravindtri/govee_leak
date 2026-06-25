"""AWS IoT MQTT client for Govee, run in a background thread.

paho-mqtt is blocking, so it lives on its own thread. Decoded readings are
handed back via a thread-safe callback (the runtime marshals onto the HA loop).
"""
from __future__ import annotations

import logging
import os
import ssl
import tempfile
import threading
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
    ) -> None:
        self._creds = creds
        self._on_readings = on_readings
        self._on_connection = on_connection
        self._client: mqtt.Client | None = None
        self._cert_file: str | None = None
        self._key_file: str | None = None
        self._stop = threading.Event()

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
            client.subscribe(self._creds.topic, qos=0)
            _LOGGER.info("Connected to Govee IoT, subscribed to %s", self._creds.topic)
            if self._on_connection:
                self._on_connection(True)
        else:
            _LOGGER.error("Govee IoT connect failed rc=%s", rc)
            if self._on_connection:
                self._on_connection(False)

    def _on_disconnect(self, _client, _userdata, rc) -> None:
        if not self._stop.is_set():
            _LOGGER.warning("Govee IoT disconnected rc=%s (will auto-reconnect)", rc)
            if self._on_connection:
                self._on_connection(False)

    def _on_message(self, _client, _userdata, msg) -> None:
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
