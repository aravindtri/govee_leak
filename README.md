# GoveeLife Water Leak — Home Assistant integration

A native Home Assistant integration for the **GoveeLife Smart Water Leak Detector 1s**
(model **H5059**) and its WiFi gateway (**H5044**). These sensors have no local API,
are not on Govee's developer API, and are not BLE/Zigbee — they talk to Govee's cloud
over AWS IoT MQTT. This integration logs into your Govee account, subscribes to that
real‑time stream, decodes the device frames, and exposes each sensor in Home Assistant.

> Unofficial. Not affiliated with or endorsed by Govee. Govee may change their cloud
> API at any time and break this integration.

## Features

- Appears in **Settings → Devices & Services** (`cloud_push`)
- One HA device per leak sensor, with:
  - **Leak** binary sensor (`moisture`)
  - **Low battery** binary sensor (`battery`, diagnostic)
  - **Battery %** sensor (diagnostic)
- Real‑time push (no polling) via the account's AWS IoT MQTT topic
- Automatic credential refresh

## Installation (HACS)

1. In HACS go to **⋮ → Custom repositories**.
2. Add this repository URL, category **Integration**.
3. Search for **GoveeLife Water Leak**, install, and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration → GoveeLife Water Leak**.

### Manual installation

Copy `custom_components/govee_leak` into your HA `config/custom_components/` folder and
restart Home Assistant.

## Configuration

The config flow asks for your **Govee Home** account email and password. Govee enforces
email two‑factor authentication, so on first setup you'll be prompted for a verification
code that Govee emails to you. Enter it to finish setup. After the first verified login
the device is "trusted", so the integration re‑authenticates in the background without
needing another code.

## How it works

1. Logs into `app2.govee.com` (v2 login + email 2FA) to obtain an account token.
2. Fetches the account's IoT key (a PKCS#12 client certificate) for mutual‑TLS.
3. Connects to AWS IoT MQTT with client id `AP/{accountId}/{random}` and subscribes to
   the account topic (`GA/…`).
4. Decodes the 20‑byte gateway frames to extract per‑sensor leak state and battery.

## Disclaimer

This project reverse‑engineers undocumented Govee endpoints for personal
interoperability. Use at your own risk.
