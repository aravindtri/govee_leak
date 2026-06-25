"""
govee_diag.py - diagnostic subscriber.

Logs in (no 2FA code needed once the client is trusted), then subscribes to BOTH
the account topic (GA/...) and every gateway topic (GD/...) discovered from the
device list, logging connection state, subscription grants, disconnects, and all
messages to captures/diag.log (flushed immediately).

Run, wait for "SUBSCRIBED", then trigger a leak sensor.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

import paho.mqtt.client as mqtt

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import govee_iot

CAP = pathlib.Path(__file__).parent / "captures"
CAP.mkdir(exist_ok=True)
LOG = CAP / "diag.log"


def log(line: str) -> None:
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    msg = f"[{stamp}] {line}"
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def gateway_topics() -> list[str]:
    topics: set[str] = set()
    p = CAP / "devices.json"
    if not p.exists():
        return []
    data = json.load(open(p, encoding="utf-8"))
    for d in data.get("devices", []):
        ds = d["deviceExt"]["deviceSettings"]
        if isinstance(ds, str):
            ds = json.loads(ds)
        gi = ds.get("gatewayInfo") or {}
        if gi.get("topic"):
            topics.add(gi["topic"])
    return sorted(topics)


def main() -> int:
    email = os.environ["GOVEE_EMAIL"]
    password = os.environ["GOVEE_PASSWORD"]
    code = os.environ.get("GOVEE_2FA_CODE") or None

    creds = govee_iot.authenticate(email, password, code)
    log(f"logged in; account topic = {creds.topic}")
    # NOTE: subscribing to GD/ gateway topics makes AWS IoT drop the connection
    # (policy only allows the account GA/ topic). Account topic only:
    extra = [t for t in os.environ.get("EXTRA_TOPICS", "").split(",") if t]
    topics = [creds.topic] + extra
    log(f"will subscribe to: {topics}")

    cert, key = govee_iot.write_temp_certs(creds)
    c = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        client_id=creds.client_id,
        protocol=mqtt.MQTTv311,
    )
    c.tls_set_context(govee_iot.build_tls_context(cert, key))

    def on_connect(cl, u, flags, rc):
        log(f"CONNACK rc={rc} ({mqtt.connack_string(rc)})")
        for t in topics:
            res, mid = cl.subscribe(t, qos=0)
            log(f"  subscribe({t}) -> res={res} mid={mid}")

    def on_subscribe(cl, u, mid, granted_qos):
        log(f"SUBACK mid={mid} granted_qos={granted_qos}  (SUBSCRIBED)")

    def on_disconnect(cl, u, rc):
        log(f"DISCONNECTED rc={rc}")

    def on_message(cl, u, msg):
        try:
            payload = json.dumps(json.loads(msg.payload), indent=2, sort_keys=True)
        except Exception:
            payload = msg.payload.decode("utf-8", "replace")
        log(f"MESSAGE topic={msg.topic}\n{payload}")

    c.on_connect = on_connect
    c.on_subscribe = on_subscribe
    c.on_disconnect = on_disconnect
    c.on_message = on_message

    log(f"connecting to {creds.endpoint}:8883")
    c.connect(creds.endpoint, 8883, keepalive=60)
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        c.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
