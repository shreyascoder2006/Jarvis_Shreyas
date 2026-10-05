"""WhatsApp / SMS front end, Twilio-shaped but provider-independent.

`handle()` is the whole product for one incoming message: text in, a list of short reply texts out.
Twilio is only a thin wrapper around it (`/twilio/webhook` turns Twilio's form fields into a call to
`handle()` and the reply into TwiML), so the same logic runs in the browser simulator, in tests and
behind a real number.

What it reuses, unchanged: the Hindi understanding (figures read by code), the assistant's tool
answers, and the guide's one-question-at-a-time conversations. What it adds: per-sender state,
a numbered menu, numbered choices (a person on a basic phone replies "2" instead of tapping a chip),
voice-note transcription, message-size limits, and Twilio request-signature checking.

Privacy: message bodies are never stored or logged; state is language + the open question, in memory,
keyed by a one-way hash of the phone number. Stock/portfolio questions are not offered over messaging
(there is no portfolio behind a phone number); the goal chart uses a labelled sample portfolio.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from xml.sax.saxutils import escape

from backend import guide, hindi_input

DEV = re.compile(r"[ऀ-ॿ]")
MAX_BODY = 1000
RATE_PER_MIN = 30
STATE_TTL = 6 * 3600

# --- wiring (set by the app) -----------------------------------------------------------------
_ctx_factory: Callable[[str], Any] | None = None
_sample: Any = None
_conn_getter: Callable[[], Any] | None = None


def configure(ctx_factory: Callable[[str], Any], conn_getter: Callable[[], Any]) -> None:
    global _ctx_factory, _conn_getter
    _ctx_factory, _conn_getter = ctx_factory, conn_getter


def _sample_portfolio():
    global _sample
    if _sample is None:
        from backend import portfolios
        from risk.portfolio import Portfolio
        d = portfolios.load(_conn_getter(), "preset_typical_retail")
        _sample = Portfolio(cash=float(d["cash"]), positions={k: int(v) for k, v in d["positions"].items()})
    return _sample


def _ctx(lang: str):
    return replace(_ctx_factory(lang), portfolio=_sample_portfolio(), convo=None)


# --- per-sender state ------------------------------------------------------------------------
@dataclass
class Sender:
    lang: str = "en"
    lang_fixed: bool = False
    choices: list[str] = field(default_factory=list)       # texts behind the numbered options on screen
    welcomed: bool = False
    seen: float = field(default_factory=time.time)
    hits: list[float] = field(default_factory=list)


SENDERS: dict[str, Sender] = {}


def sender_key(raw: str) -> str:
    """A one-way id for the phone number. The number itself is never kept."""
    n = re.sub(r"^(whatsapp:)", "", raw.strip().lower())
    return hashlib.sha256(n.encode()).hexdigest()[:16]


def channel_of(raw: str) -> str:
    return "whatsapp" if raw.strip().lower().startswith("whatsapp:") else "sms"


def _state(key: str) -> Sender:
    now = time.time()
    for k in [k for k, v in SENDERS.items() if now - v.seen > STATE_TTL]:
        SENDERS.pop(k, None)
        guide.STATE.pop("m:" + k, None)
    st = SENDERS.setdefault(key, Sender())
    st.seen = now
    return st


# --- menu ------------------------------------------------------------------------------------
MENU: list[tuple[str, str, str]] = [        # (tool, English, Hindi)
    ("loan", "Check a moneylender's interest", "साहूकार का ब्याज जाँचें"),
    ("scheme", "Is an offer or scheme real?", "क्या कोई ऑफ़र/योजना असली है?"),
    ("schemes", "Government schemes I can get", "मुझे मिलने वाली सरकारी योजनाएँ"),
    ("docs", "Are my papers ready?", "क्या मेरे काग़ज़ तैयार हैं?"),
    ("income", "Plan my money around harvest", "फ़सल के हिसाब से पैसे की योजना"),
    ("fee", "What does a fund fee cost?", "फ़ंड की फ़ीस कितनी पड़ती है?"),
    ("emergency", "How long will my savings last?", "मेरी बचत कितने महीने चलेगी?"),
    ("goal", "Will my savings reach my goal?", "क्या मेरी बचत लक्ष्य तक पहुँचेगी?"),
    ("credit", "Credit score: why was my loan rejected?", "क्रेडिट स्कोर: मेरा ऋण क्यों रिजेक्ट हुआ?"),
    ("saving", "Save a little every day for a goal", "किसी लक्ष्य के लिए रोज़ थोड़ी बचत"),
    ("hold", "Sell my crop now or wait?", "फ़सल अभी बेचूँ या रुकूँ?"),
    ("dbt", "Why has my government payment not come?", "मेरा सरकारी पैसा क्यों नहीं आया?"),
    ("upi", "Is this UPI request or QR safe?", "क्या यह UPI रिक्वेस्ट/QR सुरक्षित है?"),
    ("policy", "Is my insurance policy a good deal?", "क्या मेरी बीमा पॉलिसी अच्छा सौदा है?"),
    ("scam", "I got a suspicious call or lost money", "मुझे ठग कॉल आई या पैसे गए"),
]
DISCLAIMER = {"en": "General information, not financial advice. Never share your OTP or PIN with anyone.",
              "hi": "यह सामान्य जानकारी है, निवेश सलाह नहीं। अपना OTP या पिन कभी किसी को न बताएँ।"}


def menu_text(lang: str, first: bool = False) -> str:
    hi = lang == "hi"
    rows = [f"{i}. {h if hi else e}" for i, (_t, e, h) in enumerate(MENU, 1)]
    head = ("नमस्ते! मैं JARVIS हूँ। एक नंबर भेजिए, या अपना सवाल लिखिए/बोलिए:" if hi else
            "Hello! I am JARVIS. Send a number, or just type or record your question:")
    foot = ("हिंदी/English बदलने के लिए 'English' या 'हिंदी' लिखिए। रोकने के लिए CANCEL।" if hi else
            "Send 'हिंदी' for Hindi. Send CANCEL to stop a task, MENU to see this again.")
    out = head + "\n" + "\n".join(rows) + "\n\n" + foot
    return out + ("\n\n" + DISCLAIMER[lang] if first else "")


# --- rendering ---------------------------------------------------------------------------------
def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            out.append(cur.strip()); cur = ""
        while len(para) > limit:
            out.append(para[:limit]); para = para[limit:]
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def render_answer(a: Any, channel: str, lang: str) -> str:
    """An Answer as plain message text. WhatsApp gets the fuller version with *bold*; SMS the short one."""
    wa = channel == "whatsapp"
    b = lambda s: f"*{s}*" if wa else s            # noqa: E731
    parts = [b(a.headline)]
    bullets = [x for x in a.bullets if x][: 4 if wa else 1]
    if bullets:
        parts.append("\n".join(("• " if wa else "") + x for x in bullets))
    if wa and a.facts:
        parts.append("\n".join(f"{f['label']}: {f['value']}" for f in a.facts[:3]))
    if a.action:
        parts.append(("➡ " if wa else "") + a.action)
    text = "\n\n".join(p for p in parts if p)
    limit = 1400 if wa else (330 if lang == "hi" else 520)
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def render_ask(g: dict[str, Any], st: Sender, channel: str, lang: str) -> str:
    a = g["ask"]
    lines = [("❓ " if channel == "whatsapp" else "") + a["question"]]
    if a.get("example") and not a.get("choices"):
        lines.append(a["example"])
    st.choices = []
    if a.get("choices"):
        st.choices = [c["text"] for c in a["choices"]]
        lines.append("\n".join(f"{i}. {c['label']}" for i, c in enumerate(a["choices"], 1)))
        lines.append("नंबर भेजिए" if lang == "hi" else "Reply with a number")
    if g.get("step"):
        lines.append(f"({g['step'][0]}/{g['step'][1]}) " + ("रोकने के लिए CANCEL" if lang == "hi" else "CANCEL to stop"))
    return "\n".join(lines)


BASE: contextvars.ContextVar = contextvars.ContextVar("messaging_base", default=None)   # the in-app chat sets its own origin


def _public_link(g: dict[str, Any]) -> str:
    base = (BASE.get() or os.environ.get("PUBLIC_BASE_URL", "")).rstrip("/")
    if not base or not g.get("route"):
        return ""
    from urllib.parse import urlencode
    q = urlencode(g.get("params") or {})
    return f"{base}/#{g['route']}" + (f"?{q}" if q else "")


# --- the conversation --------------------------------------------------------------------------
GREET = re.compile(r"^\s*(hi|hello|hey|hii+|namaste|namaskar|start|menu|help|options|नमस्ते|नमस्कार|मदद|मेनू|शुरू)\W*$", re.I)
SET_HI = re.compile(r"^\s*(hindi|हिंदी|हिन्दी|hindi me|in hindi)\W*$", re.I)
SET_EN = re.compile(r"^\s*(english|अंग्रेज़ी|अंग्रेजी|in english)\W*$", re.I)
CANCEL_OR_STOP = re.compile(r"^\s*(cancel|stop|reset|exit|रद्द|रुको|बंद)\W*$", re.I)
ALLOWED = {"credit_score", "saving_goal", "hold_sell", "dbt_trace", "upi_check", "policy_check", "moneylender", "scheme_check", "entitlements", "docs_ready", "income_plan", "fee_drag", "emergency", "goal",
           "scam_help", "scam_recovery", "tip_scan", "define", "help", "chitchat", "predict", "simplify", "more"}


def _rate_ok(st: Sender) -> bool:
    now = time.time()
    st.hits = [h for h in st.hits if now - h < 60]
    st.hits.append(now)
    return len(st.hits) <= RATE_PER_MIN


async def handle(sender: str, body: str, channel: str | None = None, audio: bytes | None = None,
                  defer: Callable[[list[str]], "asyncio.Future[Any] | Any"] | None = None) -> list[str]:
    """One incoming message -> the replies to send back (already split to fit the channel).

    `defer`, if given, is called with the real answer once it is ready whenever that answer
    needed the local model: `handle()` then returns a short acknowledgement immediately instead
    of making the caller wait on Ollama synchronously (a slow transport can time out a caller
    that does not expect that). Transports that don't pass `defer` (Twilio today) are completely
    unaffected -- same synchronous behaviour as always."""
    channel = channel or channel_of(sender)
    key = sender_key(sender)
    st = _state(key)
    cid = "m:" + key
    prefix = ""
    if audio:
        text, prefix = await _transcribe(audio, st)
        if not text:
            return [prefix]
    else:
        text = (body or "").strip()[:MAX_BODY]
    if not _rate_ok(st):
        return ["Too many messages too fast. Please wait a minute. / कृपया एक मिनट रुकिए।"]
    first = not st.welcomed
    st.welcomed = True

    if DEV.search(text) and not st.lang_fixed:
        st.lang = "hi"
    if SET_HI.match(text):
        st.lang, st.lang_fixed = "hi", True
        return _pack([menu_text("hi")], channel)
    if SET_EN.match(text):
        st.lang, st.lang_fixed = "en", True
        return _pack([menu_text("en")], channel)
    lang = st.lang

    if CANCEL_OR_STOP.match(text):
        guide.STATE.pop(cid, None); st.choices = []
        return _pack([("ठीक है, रोक दिया। MENU भेजिए।" if lang == "hi" else "Okay, stopped. Send MENU to start again.")], channel)
    if not text or GREET.match(text):
        return _pack([prefix + menu_text(lang, first)], channel)

    # A numbered reply: an option of the open question, else a menu item.
    if re.fullmatch(r"\s*\d{1,2}\s*", text):
        n = int(text)
        if st.choices and 1 <= n <= len(st.choices):
            text = st.choices[n - 1]
        elif not guide.active(cid) and 1 <= n <= len(MENU):
            tool = MENU[n - 1][0]
            if tool == "scam":
                text = "I got a suspicious call asking for my OTP" if lang == "en" else "मुझे OTP माँगने वाली कॉल आई"
            else:
                return _pack([prefix + await _reply_guide(guide.begin(tool, "", lang, cid), st, channel, lang)], channel)
    st.choices = []

    token = guide.CTX.set(_ctx(lang))
    try:
        ga = guide.intercept(text, lang, cid)
        if ga is not None:
            g = ga.data["guide"]
            if g.get("tool") is None and not g.get("ask"):          # "open the X page" means nothing on a phone
                return _pack([menu_text(lang)], channel)
            return _pack([prefix + await _reply_guide(g, st, channel, lang, answer=ga)], channel)

        english = text
        if DEV.search(text):
            conv, _how = await hindi_input.convert(text)
            english = conv or text

        from backend import assistant
        if defer is not None and assistant.will_need_model(english, _ctx(lang)):
            ack = "एक मिनट, देख रहा हूँ…" if lang == "hi" else "Let me check that — one moment…"

            async def _finish() -> None:
                tok = guide.CTX.set(_ctx(lang))
                try:
                    replies = await _model_reply(english, cid, st, channel, lang, prefix)
                finally:
                    guide.CTX.reset(tok)
                await defer(replies)

            asyncio.create_task(_finish())
            return _pack([prefix + ack], channel)

        return await _model_reply(english, cid, st, channel, lang, prefix)
    finally:
        guide.CTX.reset(token)


async def _model_reply(english: str, cid: str, st: Sender, channel: str, lang: str, prefix: str) -> list[str]:
    """The part of handle() that needs assistant.aanswer() -- may call the local model. Factored
    out so the deferred (async-ack) path in handle() and the synchronous path share one
    implementation; nothing about the reply (formatting, guided follow-ups, the 'open this page'
    link) differs based on which path got here."""
    from backend import assistant
    answer = await assistant.aanswer(english, _ctx(lang))
    intent = answer.data.get("intent", "")
    kind = answer.kind
    if kind in guide.KIND_TO_TOOL and intent in ALLOWED | {"fee_drag", "emergency", "goal"}:
        g = guide.begin(guide.KIND_TO_TOOL[kind], english, lang, cid)
        if not g["done"]:
            return _pack([prefix + render_ask(g, st, channel, lang)], channel)
        out = render_answer(answer, channel, lang)
        link = _public_link(g)
        return _pack([prefix + out + (("\n\n🔗 " + link) if link and channel == "whatsapp" else "")], channel)
    if intent in ALLOWED or kind in ("scam_help", "scam_recovery", "tip_scan", "define", "help", "chitchat", "predict"):
        return _pack([prefix + render_answer(answer, channel, lang)], channel)
    note = ("यह सवाल मैं यहाँ नहीं सुलझा सकता। नीचे से चुनिए:" if lang == "hi" else "I can't help with that one here. Pick from the menu:")
    return _pack([note + "\n\n" + menu_text(lang)], channel)


async def _reply_guide(g: dict[str, Any], st: Sender, channel: str, lang: str, answer: Any = None) -> str:
    if g.get("cancelled"):
        return g["say"]
    if g.get("ask"):
        return render_ask(g, st, channel, lang)
    st.choices = []
    link = _public_link(g)
    tail = ("\n\n" + ("और जानने के लिए MENU भेजिए।" if lang == "hi" else "Send MENU for more.")) if channel == "whatsapp" else ""
    body = g.get("say") or ""
    return (("*" + body + "*") if channel == "whatsapp" and False else body) + (("\n\n🔗 " + link) if link and channel == "whatsapp" else "") + tail


def _pack(texts: list[str], channel: str) -> list[str]:
    limit = 1500 if channel == "whatsapp" else 600
    out: list[str] = []
    for t in texts:
        out.extend(_split(t, limit))
    return out


async def _transcribe(audio: bytes, st: Sender) -> tuple[str, str]:
    """A voice note becomes text first, and the text is shown back, so a mishearing can be spotted."""
    from voice import stt
    if not stt.available():
        return "", ("वॉइस नोट अभी समझ नहीं सकता, कृपया लिखकर भेजिए।" if st.lang == "hi" else "I can't read voice notes right now. Please type your question.")
    try:
        res = await asyncio.to_thread(stt.transcribe, audio, st.lang)
    except Exception:  # noqa: BLE001
        return "", ("आवाज़ साफ़ नहीं आई, फिर से भेजिए।" if st.lang == "hi" else "I couldn't make that out. Please send it again.")
    text = res.text.strip()
    if not text:
        return "", ("आवाज़ साफ़ नहीं आई, फिर से भेजिए।" if st.lang == "hi" else "I couldn't make that out. Please send it again.")
    if DEV.search(text) and not st.lang_fixed:
        st.lang = "hi"
    return text, ("🎙 " + ("मैंने सुना: " if st.lang == "hi" else "I heard: ") + f"“{text}”\n\n")


# --- Twilio plumbing ---------------------------------------------------------------------------
def twiml(messages: list[str]) -> str:
    body = "".join(f"<Message>{escape(m)}</Message>" for m in messages)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def valid_signature(url: str, params: dict[str, str], signature: str, token: str) -> bool:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + each POST param name+value, sorted by name))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    mac = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(mac, signature or "")


def status() -> dict[str, Any]:
    return {"twilio_account": bool(os.environ.get("TWILIO_ACCOUNT_SID")), "twilio_token": bool(os.environ.get("TWILIO_AUTH_TOKEN")),
            "whatsapp_from": bool(os.environ.get("TWILIO_WHATSAPP_FROM")), "sms_from": bool(os.environ.get("TWILIO_SMS_FROM")),
            "validate_signatures": os.environ.get("JARVIS_TWILIO_VALIDATE", "") == "1", "public_base_url": os.environ.get("PUBLIC_BASE_URL", "") or None,
            "voice_notes": bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"))}


async def fetch_media(url: str) -> bytes | None:
    """Voice notes sit behind the account's basic auth on Twilio."""
    import httpx
    sid, token = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        return None
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url, auth=(sid, token))
        return r.content if r.status_code == 200 else None


async def send(to: str, body: str, channel: str = "whatsapp", client: Any = None) -> dict[str, Any]:
    """Outbound message through Twilio's REST API (for reminders later). Needs the env vars above."""
    import httpx
    sid, token = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_WHATSAPP_FROM" if channel == "whatsapp" else "TWILIO_SMS_FROM")
    if not (sid and token and frm):
        return {"ok": False, "reason": "twilio not configured"}
    to_ = to if to.startswith("whatsapp:") or channel != "whatsapp" else "whatsapp:" + to
    c = client or httpx.AsyncClient(timeout=20)
    try:
        r = await c.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, token),
                         data={"To": to_, "From": frm, "Body": body})
        return {"ok": r.status_code < 300, "status": r.status_code}
    finally:
        if client is None:
            await c.aclose()
