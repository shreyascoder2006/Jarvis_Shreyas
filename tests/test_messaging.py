import asyncio
import base64
import hashlib
import hmac

import pytest

from backend import assistant, guide, messaging as M, portfolios
from backend.session import Session


@pytest.fixture(autouse=True)
def wired():
    s = Session.create(); s.reprice()
    d = portfolios.load(s.conn, "preset_typical_retail")
    s.set_portfolio(d["name"], d["cash"], d["positions"], "preset_typical_retail")
    ctx = lambda lang: assistant.Ctx(s.pit, s.portfolio, s.prices, s.policy, s.conn, None, lang)   # noqa: E731
    M.configure(ctx, lambda: s.conn)
    M.SENDERS.clear(); M._sample = None; guide.STATE.clear(); guide.LAST_SCHEMES.clear()
    yield


def say(frm, *msgs):
    out = []
    for m in msgs:
        out.append(asyncio.run(M.handle(frm, m)))
    return out


def test_greeting_returns_the_numbered_menu_and_a_disclaimer_once():
    a, b = say("whatsapp:+911", "hi", "menu")
    assert "1. Check a moneylender" in a[0] and "OTP" in a[0]
    assert "OTP or PIN" not in b[0]


def test_number_starts_a_tool_and_numbered_choices_answer_the_question():
    r = say("whatsapp:+912", "3", "42", "1", "2", "3", "3", "2")
    assert "How old" in r[0][0] and "1. Woman" in r[1][0]
    assert "You may qualify" in r[-1][0] and "PM-KISAN" in r[-1][0]


def test_loan_by_whatsapp_matches_the_calculator():
    r = say("whatsapp:+913", "my sahukar charges 5 rupees per hundred a month on 50000 for 10 months")
    assert "60% a year" in r[0][0] and "25,000" in r[0][0]


def test_hindi_is_detected_and_sticks():
    r = say("+914", "साहूकार पाँच रुपये सैकड़ा महीने पर पचास हज़ार रुपये दस महीने के लिए", "मेनू")
    assert "60%" in r[0][0] and "25,000" in r[0][0] and "ब्याज" in r[0][0]
    assert "नमस्ते" in r[1][0]
    en = say("+914", "English")
    assert "Hello" in en[0][0]


def test_sms_replies_are_short_and_plain_whatsapp_gets_more():
    sms = say("+915", "my sahukar charges 5 rupees per hundred a month on 50000 for 10 months")[0][0]
    assert len(sms) <= 700 and "*" not in sms
    q = "what is an expense ratio"
    wa = say("whatsapp:+915", q)[0][0]; sm = say("+9150", q)[0][0]
    assert len(sm) <= 530 and len(wa) >= len(sm)


def test_portfolio_questions_are_not_answered_over_messaging():
    r = say("whatsapp:+916", "can I buy HDFC Bank")[0][0]
    assert "menu" in r.lower() or "Send a number" in r


def test_cancel_clears_the_open_question():
    say("whatsapp:+917", "1")
    r = say("whatsapp:+917", "cancel")
    assert "stopped" in r[0][0]
    assert not guide.active("m:" + M.sender_key("whatsapp:+917"))


def test_phone_numbers_are_not_kept():
    say("whatsapp:+918", "hi")
    assert all("918" not in k for k in M.SENDERS) and all(len(k) == 16 for k in M.SENDERS)
    assert M.sender_key("whatsapp:+918") == M.sender_key("+918")


def test_rate_limit():
    out = []
    for _ in range(M.RATE_PER_MIN + 2):
        out.append(asyncio.run(M.handle("+919", "hi"))[0])
    assert "Too many" in out[-1]


def test_twiml_escapes_and_wraps_each_message():
    x = M.twiml(["a < b & c", "second"])
    assert x.count("<Message>") == 2 and "a &lt; b &amp; c" in x and x.startswith("<?xml")


def test_signature_validation_matches_twilios_algorithm():
    url, params, token = "https://example.com/twilio/webhook", {"From": "whatsapp:+1", "Body": "hi", "A": "1"}, "secret"
    data = url + "".join(k + params[k] for k in sorted(params))
    sig = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    assert M.valid_signature(url, params, sig, token)
    assert not M.valid_signature(url, {**params, "Body": "hello"}, sig, token)
    assert not M.valid_signature(url, params, "", token)


def test_long_text_splits_to_the_channel_limit():
    parts = M._split("para one\n\n" + "x" * 1800 + "\n\nlast", 1500)
    assert all(len(p) <= 1500 for p in parts) and len(parts) >= 2


def test_outbound_send_needs_config_and_uses_twilios_rest_endpoint(monkeypatch):
    assert asyncio.run(M.send("+1", "hi"))["ok"] is False
    for k, v in {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886"}.items():
        monkeypatch.setenv(k, v)
    seen = {}

    class Fake:
        async def post(self, url, auth, data):
            seen.update(url=url, auth=auth, data=data)
            return type("R", (), {"status_code": 201})()
    r = asyncio.run(M.send("+919800000000", "hello", "whatsapp", client=Fake()))
    assert r["ok"] and seen["url"].endswith("/Accounts/AC1/Messages.json") and seen["data"]["To"] == "whatsapp:+919800000000"


def test_in_app_chat_gets_a_link_back_to_the_page_with_the_result():
    tok = M.BASE.set("http://localhost:8000")
    try:
        r = say("whatsapp:+9199", "my sahukar charges 5 rupees per hundred a month on 50000 for 10 months")[0][0]
    finally:
        M.BASE.reset(tok)
    assert "🔗 http://localhost:8000/#/rural?tool=loan" in r


def test_outbound_sms_send_needs_config_and_uses_the_gateways_rest_api(monkeypatch):
    from backend import sms_gateway

    assert asyncio.run(sms_gateway.send("+1", "hi"))["ok"] is False
    monkeypatch.setenv("SMS_GATEWAY_URL", "http://192.168.1.50:8080")
    monkeypatch.setenv("SMS_GATEWAY_USER", "u")
    monkeypatch.setenv("SMS_GATEWAY_PASS", "p")
    seen = {}

    class Fake:
        async def post(self, url, auth, json):
            seen.update(url=url, auth=auth, json=json)
            return type("R", (), {"status_code": 200})()
    r = asyncio.run(sms_gateway.send("+919800000000", "hello", client=Fake()))
    assert r["ok"] and seen["url"] == "http://192.168.1.50:8080/message" and seen["auth"] == ("u", "p")
    assert seen["json"]["phoneNumbers"] == ["+919800000000"]


def test_bridge_sms_endpoint_routes_fast_messages_synchronously_and_dedupes_retries():
    from fastapi.testclient import TestClient
    from backend.app import app

    with TestClient(app) as c:
        r1 = c.post("/bridge/sms", json={"payload": {
            "messageId": "m1", "message": "hi", "phoneNumber": "+919800000099"}})
        assert r1.json()["ok"] and "JARVIS" in r1.json()["messages"][0]

        r2 = c.post("/bridge/sms", json={"payload": {
            "messageId": "m1", "message": "hi", "phoneNumber": "+919800000099"}})
        assert r2.json() == {"ok": True, "deduped": True}


def test_bridge_sms_endpoint_acknowledges_immediately_when_the_model_is_needed():
    from fastapi.testclient import TestClient
    from backend.app import app

    with TestClient(app) as c:
        r = c.post("/bridge/sms", json={"payload": {
            "messageId": "m2",
            "message": "honestly not sure if im overpaying on fees across my stuff, can you have a look",
            "phoneNumber": "+919800000099",
        }})
    body = r.json()
    assert body["ok"] and len(body["messages"]) == 1 and "moment" in body["messages"][0].lower()
