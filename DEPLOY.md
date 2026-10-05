# Deploying JARVIS, with the WhatsApp bridge and SMS gateway, to a real server

This is the always-on counterpart to `docs/SETUP.md` (which covers running the app locally
for development). It assumes a fresh Linux VPS with Docker and Docker Compose installed.

## 1. Size the box

Ollama runs `llama3.1:8b` (Q4_K_M, ~4.9GB on disk) on CPU. **≥8GB RAM, 4 vCPU** is the
realistic minimum for tolerable response times — this is not a box you can run on a free-tier
micro instance. Any mainstream VPS provider's "general purpose, 8GB" tier works; which one is
your call, this isn't something to over-specify in a document that will go stale.

## 2. Get the code and your secrets onto the box

```bash
git clone <this repo> jarvis && cd jarvis
cp .env.example .env            # fill in whatever you actually use (Twilio is optional --
                                 # the WhatsApp bridge below needs none of it)
```

`.env` and `whatsapp-bridge/auth/` are both gitignored for a reason — never commit either.

## 3. Bring up the stack

```bash
docker compose up -d --build
```

This starts `ollama`, `app` (the FastAPI backend + built frontend), `whatsapp-bridge`, and
`caddy` (reverse proxy / automatic HTTPS). `app` and `whatsapp-bridge` will both be running
but not yet useful — the backend has no models pulled and no data snapshot, and the bridge
has no linked WhatsApp session. Both are fixed by the one-time steps below.

## 4. One-time init: pull the Ollama models

```bash
docker compose run --rm ollama-pull
```

Pulls `llama3.1:8b` and `phi3:mini` into the `ollama-models` volume. Re-running later is
harmless — `ollama pull` no-ops if a model is already present.

## 5. One-time init: the data snapshot and voice models

Either build it on the server (~5 minutes, network-dependent):

```bash
docker compose exec app python tools/ingest.py
docker compose exec app python tools/get_voice.py      # ~400MB, one time
```

...or, faster and more predictable, build it locally first (`docs/SETUP.md`) and copy the
known-good result over:

```bash
docker compose cp data/snapshot.db   app:/app/data/snapshot.db
docker compose cp models/            app:/app/models/
```

## 6. One-time init: link the WhatsApp bridge

Needs a terminal attached, since it may ask for the phone number and always prints the
pairing code to the console:

```bash
docker compose run --rm --service-ports whatsapp-bridge
```

Set `WHATSAPP_PAIR_NUMBER=<country code + digits>` in `.env` first to skip the interactive
prompt. Enter the printed code promptly — WhatsApp's pairing codes expire within a couple of
minutes, and letting one sit too long risks the socket cycling into a bad state (seen in
testing: a "stream error" / "logged out" loop that needs `whatsapp-bridge/auth/` wiped and a
fresh code requested — annoying but recoverable, never destructive to anything else).

Once linked, stop that foreground run (Ctrl-C) and bring the service up normally:

```bash
docker compose up -d whatsapp-bridge
```

The session lives in the `bridge-auth` volume from here on — restarts and redeploys don't
need re-pairing.

## 7. Point a domain at it (optional, needed only for the public web app)

Edit `Caddyfile`, replace the `:80` placeholder with your real domain, `docker compose
restart caddy`. The WhatsApp side needs no public URL at all — the bridge connects outbound
to WhatsApp's own servers; nothing needs to reach this box from the internet for messaging
to work, only for the browser-facing web app.

## 8. Verify

```bash
curl https://your-domain.example/health        # or http://<server-ip> if no domain yet
docker compose logs -f whatsapp-bridge          # should show "connected to WhatsApp"
```

Send a real WhatsApp message to the linked number and confirm a reply arrives.

## 9. Optional: a true SMS channel, no Twilio, no internet beyond the LAN

Unlike the WhatsApp bridge, this needs no extra container — it's two endpoints already built
into the backend (`backend/sms_gateway.py`, `POST /bridge/sms` in `backend/app.py`), talking
directly to a spare Android phone's own REST API over your local network.

1. On a spare Android 5.0+ phone with an active SMS-capable SIM, install **SMS Gateway for
   Android** ([capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway),
   Apache 2.0) and enable **local/private mode** — no account, no cloud, "all data stays on
   your device."
2. **Exempt it from battery optimization** (Settings → Apps → the gateway app → Battery → Don't
   optimize). Not documented by the app itself, but a near-universal Android gotcha, especially
   on Xiaomi/Oppo/Vivo-style skins: without this, the OS can silently kill a background app and
   incoming texts stop being answered with no obvious symptom.
3. Put the phone on the **same WiFi/LAN** as this server. Note the server's **LAN IP** (not
   `localhost`, not the public domain) and confirm the firewall allows inbound on port 8000 from
   the LAN — on a Windows dev box specifically, this is usually the actual blocker.
4. Register the webhook once, from any machine on the LAN:
   ```bash
   curl -u <gateway-user>:<gateway-pass> -H "Content-Type: application/json" \
     -d '{"url":"http://<server-lan-ip>:8000/bridge/sms","event":"sms:received"}' \
     http://<phone-lan-ip>:8080/webhooks
   ```
5. Add to `.env` and restart the backend:
   ```
   SMS_GATEWAY_URL=http://<phone-lan-ip>:8080
   SMS_GATEWAY_USER=<gateway-user>
   SMS_GATEWAY_PASS=<gateway-pass>
   ```
6. Send a real text to the gateway phone's number from another phone and confirm a reply
   arrives — try a simple question first, then an ambiguous one to see the "let me check…" ack
   followed by the real answer.

**One real trade-off, worth knowing going in**: the WhatsApp bridge is a process independent of
this backend, so if the backend goes down it can still apologize instead of going silent. The
SMS path has no such independent process — if the backend is down when a text arrives, nothing
can reply, because the thing that would reply is the thing that's down. Mitigate by keeping the
backend under `restart: unless-stopped` (already the case here) rather than running it by hand.

## Day to day

- `docker compose logs -f app` / `whatsapp-bridge` — tail logs.
- `docker compose restart app` — pick up a code change (rebuild first: `docker compose up -d
  --build app`).
- The bridge auto-reconnects on a dropped connection; it only needs re-pairing if WhatsApp
  actually logs the session out (rare, and the log says so explicitly when it happens).
- The SMS gateway needs no equivalent reconnect handling — it's stateless REST both ways, so
  there's nothing to reconnect.
