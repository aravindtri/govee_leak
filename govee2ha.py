"""
govee2ha.py - Bridge GoveeLife H5059 water-leak sensors -> Home Assistant.

Connects to your Govee account's AWS IoT MQTT stream (the same real-time feed the
Govee Home app uses), decodes the H5044 gateway frames, and republishes each
H5059 leak sensor to your Home Assistant MQTT broker using MQTT Discovery:

  - binary_sensor (device_class: moisture)  -> Wet / Dry
  - sensor        (device_class: battery)   -> battery %
  - binary_sensor (device_class: battery)   -> Low battery

Entities are auto-created in HA and grouped per physical sensor. State + battery
update live as the gateway reports them. Availability is backed by an MQTT LWT.

Config via environment / .env (see .env.example):
  GOVEE_EMAIL, GOVEE_PASSWORD, GOVEE_2FA_CODE (only when first prompted)
  MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_DISCOVERY_PREFIX
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import govee_iot
import govee_protocol as proto

DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
BASE = "govee_leak"
AVAIL_TOPIC = f"{BASE}/bridge/availability"
LOW_BATTERY_PCT = int(os.environ.get("LOW_BATTERY_PCT", "20"))
DEVICE_LIST_URL = "https://app2.govee.com/device/rest/devices/v1/list"

_sensors: dict[str, proto.LeakSensor] = {}
_by_gateway_sno: dict[tuple[str, int], proto.LeakSensor] = {}
_announced: set[str] = set()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# --------------------------------------------------------------------------- #
# Govee device list (sensor names + sno mapping + initial battery)
# --------------------------------------------------------------------------- #
def fetch_device_list(email: str, token: str) -> dict:
    headers = govee_iot._common_headers(email)
    headers["Authorization"] = f"Bearer {token}"
    r = requests.post(DEVICE_LIST_URL, headers=headers, json={}, timeout=30)
    r.raise_for_status()
    return r.json()


def load_sensors(email: str, token: str) -> None:
    data = fetch_device_list(email, token)
    for s in proto.parse_leak_sensors(data):
        _sensors[s.device] = s
        if s.gateway_device and s.sno >= 0:
            _by_gateway_sno[(s.gateway_device, s.sno)] = s
    print(f"[govee] discovered {len(_sensors)} H5059 leak sensor(s)")


# --------------------------------------------------------------------------- #
# Home Assistant MQTT discovery + state
# --------------------------------------------------------------------------- #
def announce(ha: mqtt.Client, s: proto.LeakSensor) -> None:
    if s.device in _announced:
        return
    uid = slug(s.device)
    state_topic = f"{BASE}/{uid}/state"
    avail = [
        {"topic": AVAIL_TOPIC},
        {"topic": f"{BASE}/{uid}/availability"},
    ]
    device_block = {
        "identifiers": [f"govee_leak_{uid}"],
        "name": s.name,
        "manufacturer": "Govee",
        "model": f"{proto.LEAK_SKU} (via {s.gateway_sku or 'gateway'})",
    }
    configs = {
        f"{DISCOVERY_PREFIX}/binary_sensor/govee_leak_{uid}/leak/config": {
            "name": "Leak",
            "unique_id": f"govee_leak_{uid}_leak",
            "device_class": "moisture",
            "state_topic": state_topic,
            "value_template": "{{ value_json.leak }}",
            "payload_on": "wet",
            "payload_off": "dry",
            "availability": avail,
            "availability_mode": "all",
            "device": device_block,
        },
        f"{DISCOVERY_PREFIX}/sensor/govee_leak_{uid}/battery/config": {
            "name": "Battery",
            "unique_id": f"govee_leak_{uid}_battery",
            "device_class": "battery",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "entity_category": "diagnostic",
            "state_topic": state_topic,
            "value_template": "{{ value_json.battery }}",
            "availability": avail,
            "availability_mode": "all",
            "device": device_block,
        },
        f"{DISCOVERY_PREFIX}/binary_sensor/govee_leak_{uid}/low_battery/config": {
            "name": "Low battery",
            "unique_id": f"govee_leak_{uid}_low_battery",
            "device_class": "battery",
            "entity_category": "diagnostic",
            "state_topic": state_topic,
            "value_template": "{{ value_json.low_battery }}",
            "payload_on": "1",
            "payload_off": "0",
            "availability": avail,
            "availability_mode": "all",
            "device": device_block,
        },
    }
    for topic, cfg in configs.items():
        ha.publish(topic, json.dumps(cfg), qos=1, retain=True)
    ha.publish(f"{BASE}/{uid}/availability", "online", qos=1, retain=True)
    _announced.add(s.device)


def _state_payload(leak: bool | None, battery: int | None) -> dict:
    state: dict = {}
    if leak is not None:
        state["leak"] = "wet" if leak else "dry"
    if battery is not None:
        state["battery"] = battery
        state["low_battery"] = 1 if battery <= LOW_BATTERY_PCT else 0
    return state


def publish_state(
    ha: mqtt.Client, s: proto.LeakSensor, leak: bool | None, battery: int | None
) -> None:
    state = _state_payload(leak, battery)
    if not state:
        return
    uid = slug(s.device)
    ha.publish(f"{BASE}/{uid}/state", json.dumps(state), qos=1, retain=True)
    ha.publish(f"{BASE}/{uid}/availability", "online", qos=1, retain=True)
    print(f"[ha] {s.name} ({uid}) -> {state}", flush=True)


def seed_initial_states(ha: mqtt.Client) -> None:
    """Announce all sensors and seed battery + assumed-dry so entities aren't unknown."""
    for s in _sensors.values():
        announce(ha, s)
        publish_state(ha, s, leak=False, battery=s.battery)


# --------------------------------------------------------------------------- #
# MQTT clients
# --------------------------------------------------------------------------- #
def make_ha_client() -> mqtt.Client:
    c = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1, client_id="govee2ha"
    )
    user = os.environ.get("MQTT_USERNAME")
    if user:
        c.username_pw_set(user, os.environ.get("MQTT_PASSWORD") or "")
    c.will_set(AVAIL_TOPIC, "offline", qos=1, retain=True)
    host = os.environ.get("MQTT_HOST", "127.0.0.1")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    c.connect(host, port, keepalive=60)
    c.loop_start()
    c.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
    print(f"[ha] connected to broker {host}:{port}")
    return c


def run_govee(ha: mqtt.Client, creds: govee_iot.GoveeIotCreds) -> None:
    cert, key = govee_iot.write_temp_certs(creds)
    gv = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        client_id=creds.client_id,
        protocol=mqtt.MQTTv311,
    )
    gv.tls_set_context(govee_iot.build_tls_context(cert, key))

    def on_connect(c, u, flags, rc):
        if rc == 0:
            c.subscribe(creds.topic, qos=0)
            print(f"[govee] connected + subscribed to {creds.topic}", flush=True)
        else:
            print(f"[govee] connect failed rc={rc}", flush=True)

    def on_message(c, u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        parsed = proto.readings_from_message(payload)
        if not parsed:
            return
        gateway, readings = parsed
        for r in readings:
            s = _by_gateway_sno.get((gateway, r.sno))
            if not s:
                continue
            announce(ha, s)
            publish_state(ha, s, leak=r.leak, battery=r.battery)

    gv.on_connect = on_connect
    gv.on_message = on_message
    gv.connect(creds.endpoint, 8883, keepalive=60)
    gv.loop_forever(retry_first_connection=True)


# --------------------------------------------------------------------------- #
def main() -> int:
    email = os.environ.get("GOVEE_EMAIL")
    password = os.environ.get("GOVEE_PASSWORD")
    code = os.environ.get("GOVEE_2FA_CODE") or None
    if not email or not password:
        print("ERROR: set GOVEE_EMAIL and GOVEE_PASSWORD (see .env.example)")
        return 2

    print(f"[govee] logging in as {email} ...")
    try:
        token, _aid, _topic = govee_iot.login(email, password, code)
    except govee_iot.NeedsVerificationCode:
        govee_iot.request_verification_code(email)
        print("[govee] 2FA required; a code was emailed to you.")
        print("        Put it in .env as GOVEE_2FA_CODE and re-run (one-time).")
        return 3

    load_sensors(email, token)
    creds = govee_iot.authenticate(email, password, code)

    ha = make_ha_client()
    seed_initial_states(ha)

    while True:
        try:
            run_govee(ha, creds)
        except KeyboardInterrupt:
            print("\nstopping")
            ha.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
            ha.loop_stop()
            return 0
        except Exception as e:
            print(f"[govee] connection error: {e!r}; re-authenticating in 30s", flush=True)
            time.sleep(30)
            try:
                creds = govee_iot.authenticate(email, password)
            except Exception as e2:
                print(f"[govee] re-auth failed: {e2!r}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
