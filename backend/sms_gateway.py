"""Outbound SMS through a local gateway phone (capcom6/android-sms-gateway, local/private mode) --
no Twilio, no cloud, no internet beyond the LAN between this backend and the phone. Mirrors
messaging.send()'s Twilio REST helper so the two read as the same pattern: config missing ->
{"ok": False}, never an exception a caller has to guard against separately.
"""
from __future__ import annotations

import os
from typing import Any


def configured() -> bool:
    return bool(os.environ.get("SMS_GATEWAY_URL") and os.environ.get("SMS_GATEWAY_USER")
                and os.environ.get("SMS_GATEWAY_PASS"))


async def send(to: str, text: str, client: Any = None) -> dict[str, Any]:
    """One SMS out, via the gateway phone's own REST API."""
    import httpx

    url = os.environ.get("SMS_GATEWAY_URL", "").rstrip("/")
    user, pw = os.environ.get("SMS_GATEWAY_USER"), os.environ.get("SMS_GATEWAY_PASS")
    if not (url and user and pw):
        return {"ok": False, "reason": "sms gateway not configured"}
    c = client or httpx.AsyncClient(timeout=20)
    try:
        r = await c.post(f"{url}/message", auth=(user, pw),
                          json={"textMessage": {"text": text}, "phoneNumbers": [to]})
        return {"ok": r.status_code < 300, "status": r.status_code}
    finally:
        if client is None:
            await c.aclose()
