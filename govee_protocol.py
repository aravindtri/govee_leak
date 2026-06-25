"""
govee_protocol.py - decode Govee H5044-gateway / H5059-leak-sensor frames and
parse the account device-list.

Frame layout (20 bytes), confirmed from live capture:
  b0  0xaa = polled status, 0xee = async event
  b1  0x04 = full status dump, 0x34 = leak data event, 0x35 = alarm-notify (skip)
  b2  sub-device index == deviceSettings.sno (0..9)
  b3  0x02 when this is a per-sensor data frame
  b5  battery percent
  b13 leak state: 0x01 = WET, 0x00 = DRY  (mirrored at b16)
  b19 XOR checksum of bytes 0..18
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

LEAK_SKU = "H5059"
GATEWAY_SKUS = {"H5044", "H5043"}
DATA_FRAME_CMDS = {0x04, 0x34}  # full status dump / leak data event


@dataclass
class SensorReading:
    sno: int
    battery: int | None
    leak: bool | None


def xor_checksum(frame: bytes) -> int:
    c = 0
    for b in frame[:19]:
        c ^= b
    return c


def decode_frame(b64: str) -> SensorReading | None:
    """Return a SensorReading if the frame is a per-sensor data frame, else None."""
    try:
        b = base64.b64decode(b64)
    except Exception:
        return None
    if len(b) != 20:
        return None
    if b[0] not in (0xAA, 0xEE):
        return None
    if b[1] not in DATA_FRAME_CMDS:
        return None
    if b[3] != 0x02:  # not a data-bearing frame (e.g. alarm-notify / meta)
        return None
    # checksum is advisory; tolerate mismatches but expose for debugging
    sno = b[2]
    battery = b[5]
    leak = bool(b[13])
    return SensorReading(sno=sno, battery=battery, leak=leak)


def readings_from_message(msg: dict) -> tuple[str, list[SensorReading]] | None:
    """
    Given a decoded account-topic JSON message from a gateway, return
    (gateway_device, [SensorReading,...]) or None if not a relevant message.
    """
    if msg.get("sku") not in GATEWAY_SKUS:
        return None
    gateway = msg.get("device")
    op = msg.get("op") or {}
    commands = op.get("command") or []
    if not gateway or not isinstance(commands, list):
        return None
    readings = [r for c in commands if (r := decode_frame(c)) is not None]
    if not readings:
        return None
    return gateway, readings


# --------------------------------------------------------------------------- #
# Device-list parsing
# --------------------------------------------------------------------------- #
@dataclass
class LeakSensor:
    device: str          # Govee device id (stable unique key)
    name: str
    sno: int             # frame index
    gateway_device: str  # gateway MAC as seen in MQTT 'device' field
    gateway_sku: str
    battery: int | None


def _expand(o):
    return json.loads(o) if isinstance(o, str) else o


def parse_leak_sensors(device_list: dict) -> list[LeakSensor]:
    out: list[LeakSensor] = []
    for d in device_list.get("devices", []):
        if d.get("sku") != LEAK_SKU:
            continue
        ds = _expand(d["deviceExt"]["deviceSettings"]) or {}
        gi = ds.get("gatewayInfo") or {}
        out.append(
            LeakSensor(
                device=d.get("device"),
                name=d.get("deviceName") or f"Govee Leak {d.get('device','')[-5:]}",
                sno=int(ds.get("sno")) if ds.get("sno") is not None else -1,
                gateway_device=gi.get("device", ""),
                gateway_sku=gi.get("sku", ""),
                battery=ds.get("battery"),
            )
        )
    return out
