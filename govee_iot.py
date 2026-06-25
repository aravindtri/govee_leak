"""
Shared Govee account/AWS-IoT helper.

Replicates the Govee Home app's undocumented auth flow so we can subscribe to the
account's real-time AWS IoT MQTT topic (the same stream the app uses for live
device events, including water-leak sensors that are NOT exposed by the public
developer API).

Flow (derived from wez/govee2mqtt src/undoc_api.rs):
  1. POST account/rest/account/v1/login  -> token, account MQTT topic
  2. GET  app/v1/account/iot/key         -> AWS IoT endpoint + p12 client cert
  3. Decode p12 -> PEM cert + key for mutual-TLS MQTT to <endpoint>:8883
"""
from __future__ import annotations

import base64
import os
import ssl
import tempfile
import time
import uuid
from dataclasses import dataclass

import requests
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

APP_VERSION = "7.4.10"
USER_AGENT = (
    f"GoveeHome/{APP_VERSION} (com.ihoment.GoVeeSensor; build:8; iOS 26.5.0) "
    "Alamofire/5.11.0"
)
# Govee enforced email 2FA in mid-2026: login moved to v2 and a verification
# code (requested via the /v1/verification endpoint) is required.
LOGIN_URL = "https://app2.govee.com/account/rest/account/v2/login"
VERIFICATION_URL = "https://app2.govee.com/account/rest/account/v1/verification"
IOT_KEY_URL = "https://app2.govee.com/app/v1/account/iot/key"


class NeedsVerificationCode(RuntimeError):
    """Raised when Govee requires an emailed 2FA code (HTTP body status 454)."""


def _client_id(email: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, email).hex


def _ms_timestamp() -> str:
    return str(int(time.time() * 1000))


@dataclass
class GoveeIotCreds:
    token: str
    account_id: int
    topic: str          # account-level MQTT topic to subscribe to
    endpoint: str       # AWS IoT endpoint host
    cert_pem: bytes     # client certificate (PEM)
    key_pem: bytes      # client private key (PEM)
    client_id: str      # mqtt client id (we reuse the app client id)


def _common_headers(email: str) -> dict:
    return {
        "appVersion": APP_VERSION,
        "clientId": _client_id(email),
        "clientType": "1",
        "iotVersion": "0",
        "timestamp": _ms_timestamp(),
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


def request_verification_code(email: str) -> None:
    """Ask Govee to email a 2FA verification code to the account."""
    headers = _common_headers(email)
    body = {"type": 8, "email": email}
    r = requests.post(VERIFICATION_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") not in (200, None):
        raise RuntimeError(
            f"Failed to request verification code: {data.get('message', data)}"
        )


def login(email: str, password: str, code: str | None = None) -> tuple[str, int, str]:
    """Return (token, account_id, account_topic).

    Raises NeedsVerificationCode if Govee responds 454 and no code was supplied.
    """
    cid = _client_id(email)
    headers = _common_headers(email)
    body = {"email": email, "password": password, "client": cid}
    if code:
        body["code"] = code
    r = requests.post(LOGIN_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    status = data.get("status")
    if status == 454:
        if not code:
            raise NeedsVerificationCode(
                "Govee requires an email verification code (2FA)."
            )
        raise RuntimeError(
            f"Verification code rejected: {data.get('message') or 'invalid/expired code'}"
        )
    if status != 200 or "client" not in data:
        raise RuntimeError(f"Login failed (status {status}): {data.get('message', data)}")
    c = data["client"]
    return c["token"], int(c["accountId"]), c["topic"]


def get_iot_key(email: str, token: str) -> tuple[str, str, str]:
    """Return (endpoint, p12_base64, p12_pass)."""
    headers = _common_headers(email)
    headers["Authorization"] = f"Bearer {token}"
    r = requests.get(IOT_KEY_URL, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != 200 or "data" not in data:
        raise RuntimeError(f"iot/key failed: {data.get('message', data)}")
    d = data["data"]
    return d["endpoint"], d["p12"], d["p12Pass"]


def p12_to_pem(p12_b64: str, p12_pass: str) -> tuple[bytes, bytes]:
    """Decode a base64 PKCS#12 blob into (cert_pem, key_pem)."""
    raw = base64.b64decode(p12_b64)
    key, cert, _chain = pkcs12.load_key_and_certificates(
        raw, p12_pass.encode("utf-8")
    )
    if cert is None or key is None:
        raise RuntimeError("p12 did not contain both a cert and a key")
    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    )
    return cert_pem, key_pem


def authenticate(email: str, password: str, code: str | None = None) -> GoveeIotCreds:
    token, account_id, topic = login(email, password, code)
    endpoint, p12_b64, p12_pass = get_iot_key(email, token)
    cert_pem, key_pem = p12_to_pem(p12_b64, p12_pass)
    # AWS IoT's policy on the Govee client cert only authorizes MQTT client ids
    # of the form "AP/{accountId}/{random}". Using anything else (e.g. the
    # account/email client id) makes the broker drop the connection (rc=7).
    mqtt_client_id = f"AP/{account_id}/{uuid.uuid4().hex[:16]}"
    return GoveeIotCreds(
        token=token,
        account_id=account_id,
        topic=topic,
        endpoint=endpoint,
        cert_pem=cert_pem,
        key_pem=key_pem,
        client_id=mqtt_client_id,
    )


def write_temp_certs(creds: GoveeIotCreds) -> tuple[str, str]:
    """Write cert/key to temp files (paho needs file paths). Returns (cert, key)."""
    tmpdir = tempfile.mkdtemp(prefix="govee_iot_")
    cert_path = os.path.join(tmpdir, "client.crt")
    key_path = os.path.join(tmpdir, "client.key")
    with open(cert_path, "wb") as f:
        f.write(creds.cert_pem)
    with open(key_path, "wb") as f:
        f.write(creds.key_pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return cert_path, key_path


def build_tls_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    # AWS IoT presents a valid public CA chain; keep verification on.
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
