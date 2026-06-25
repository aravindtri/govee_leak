"""
govee_capture.py - Govee account AWS-IoT MQTT sniffer.

Connects to your Govee account's real-time MQTT topic and prints EVERY message it
receives. Use this to discover the JSON payload your water-leak sensors emit:
run it, then physically trigger a sensor (dip the probes in water, then dry them)
and watch for the leak/battery messages. Also captures periodic heartbeats.

Usage:
  set GOVEE_EMAIL / GOVEE_PASSWORD (e.g. via .env) then:
    python govee_capture.py

All raw messages are also appended to captures/govee_raw.log for later analysis.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

import paho.mqtt.client as mqtt

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import govee_iot

CAPTURE_DIR = pathlib.Path(__file__).parent / "captures"
CAPTURE_DIR.mkdir(exist_ok=True)
LOG_PATH = CAPTURE_DIR / "govee_raw.log"


def _log(line: str) -> None:
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    email = os.environ.get("GOVEE_EMAIL")
    password = os.environ.get("GOVEE_PASSWORD")
    code = os.environ.get("GOVEE_2FA_CODE") or None
    if not email or not password:
        print("ERROR: set GOVEE_EMAIL and GOVEE_PASSWORD (see .env.example)")
        return 2

    print(f"Logging in as {email} ...")
    try:
        creds = govee_iot.authenticate(email, password, code)
    except govee_iot.NeedsVerificationCode:
        govee_iot.request_verification_code(email)
        print("Govee requires a 2FA code; one was just emailed to you.")
        print("Set GOVEE_2FA_CODE in .env (or env) and re-run.")
        return 3
    print(f"  account_id : {creds.account_id}")
    print(f"  iot endpoint: {creds.endpoint}")
    print(f"  account topic: {creds.topic}")
    print(f"Logging all messages to {LOG_PATH}")

    cert_path, key_path = govee_iot.write_temp_certs(creds)

    client = mqtt.Client(client_id=creds.client_id, protocol=mqtt.MQTTv311)
    client.tls_set_context(govee_iot.build_tls_context(cert_path, key_path))

    def on_connect(c, userdata, flags, rc, *args):
        if rc == 0:
            print("Connected to Govee AWS IoT. Subscribing to account topic...")
            c.subscribe(creds.topic, qos=0)
            print("Subscribed. Now TRIGGER A SENSOR (dip probes in water) and watch below.\n")
        else:
            print(f"Connect failed rc={rc}")

    def on_message(c, userdata, msg):
        ts = dt.datetime.now().isoformat(timespec="seconds")
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            pretty = json.dumps(payload, indent=2, sort_keys=True)
        except Exception:
            pretty = msg.payload.decode("utf-8", "replace")
        _log(f"\n===== {ts}  topic={msg.topic} =====\n{pretty}")

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {creds.endpoint}:8883 ...")
    client.connect(creds.endpoint, 8883, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nStopping (Ctrl-C).")
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
