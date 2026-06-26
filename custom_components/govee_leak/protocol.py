"""Govee H5044-gateway / H5059-leak-sensor frame + device-list decoding.

20-byte frame (confirmed against live capture):
  b0  0xaa polled status, 0xee async event
  b1  0x04 full status dump, 0x34 leak data event, 0x35 alarm-notify (skip)
  b2  sub-device index == deviceSettings.sno (0..9)
  b3  0x02 when this is a per-sensor data frame
  b5  battery percent
  b13 leak state (older firmware only; unreliable, part of a rolling counter)
  b16 leak state mirror: 0x01 wet, 0x00 dry (reliable across firmware versions)
  b19 XOR checksum of bytes 0..18
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from .const import GATEWAY_SKUS, LEAK_SKU

DATA_FRAME_CMDS = {0x04, 0x34}


@dataclass
class SensorReading:
    """A decoded per-sensor data frame."""

    sno: int
    battery: int | None
    leak: bool | None


def decode_frame(b64: str) -> SensorReading | None:
    """Return a SensorReading for per-sensor data frames, else None."""
    try:
        b = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if len(b) != 20 or b[0] not in (0xAA, 0xEE) or b[1] not in DATA_FRAME_CMDS:
        return None
    if b[3] != 0x02:
        return None
    # b16 is the reliable leak mirror; b13 only matches on older firmware.
    return SensorReading(sno=b[2], battery=b[5], leak=bool(b[16]))


def readings_from_message(msg: dict) -> tuple[str, list[SensorReading]] | None:
    """Return (gateway_device, [SensorReading,...]) from an account-topic message."""
    if msg.get("sku") not in GATEWAY_SKUS:
        return None
    gateway = msg.get("device")
    commands = (msg.get("op") or {}).get("command") or []
    if not gateway or not isinstance(commands, list):
        return None
    readings = [r for c in commands if (r := decode_frame(c)) is not None]
    if not readings:
        return None
    return gateway, readings


@dataclass
class LeakSensor:
    """A physical H5059 leak sensor discovered from the account device list."""

    device: str
    name: str
    sno: int
    gateway_device: str
    gateway_sku: str
    gateway_topic: str
    battery: int | None


def _expand(o):
    return json.loads(o) if isinstance(o, str) else o


def parse_leak_sensors(device_list: dict) -> list[LeakSensor]:
    """Extract H5059 leak sensors from the device-list response."""
    out: list[LeakSensor] = []
    for d in device_list.get("devices", []):
        if d.get("sku") != LEAK_SKU:
            continue
        ds = _expand(d["deviceExt"]["deviceSettings"]) or {}
        gi = ds.get("gatewayInfo") or {}
        sno = ds.get("sno")
        out.append(
            LeakSensor(
                device=d.get("device"),
                name=d.get("deviceName") or f"Govee Leak {str(d.get('device'))[-5:]}",
                sno=int(sno) if sno is not None else -1,
                gateway_device=gi.get("device", ""),
                gateway_sku=gi.get("sku", ""),
                gateway_topic=gi.get("topic", ""),
                battery=ds.get("battery"),
            )
        )
    return out
