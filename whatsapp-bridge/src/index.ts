/** Links a real WhatsApp number (via Baileys, an unofficial client -- see docs/MESSAGING.md for
 *  the risk this carries) and forwards every inbound message to the Python backend's
 *  /bridge/whatsapp, exactly the way /twilio/webhook does for Twilio. The backend decides what
 *  to say; this file only ever moves bytes between WhatsApp and it.
 *
 *  One-time setup: run this with a terminal attached (`npm run pair`), it asks for the phone
 *  number to link (or set WHATSAPP_PAIR_NUMBER to skip the prompt) and prints a pairing code.
 *  Enter that under WhatsApp -> Linked Devices -> Link a Device -> Link with phone number
 *  instead. After that, the session lives in WHATSAPP_AUTH_DIR and reconnects on its own.
 */
import readline from "node:readline";

import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import pino from "pino";

import { startSendServer } from "./server.js";

type Socket = ReturnType<typeof makeWASocket>;

const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || "./auth";
const BACKEND_URL = (process.env.JARVIS_BACKEND_URL || "http://localhost:8000").replace(/\/$/, "");
const BRIDGE_PORT = Number(process.env.BRIDGE_PORT || 3000);
const PAIR_NUMBER = process.env.WHATSAPP_PAIR_NUMBER || "";
// This instance's own callback address, sent with every inbound forward so the backend's
// deferred replies come back here and not to some other linked number's bridge. In Docker each
// bridge is its own service with its own name (set this explicitly per instance); the
// localhost default only makes sense for a single bridge running outside a container.
const SELF_URL = (process.env.BRIDGE_SELF_URL || `http://localhost:${BRIDGE_PORT}`).replace(/\/$/, "");

const logger = pino({ level: process.env.LOG_LEVEL || "warn" });

let currentSocket: Socket | null = null;

function askForNumber(): Promise<string> {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const q = (text: string) => new Promise<string>((resolve) => rl.question(text, resolve));
  return q("Phone number to link, country code + digits only (e.g. 919876543210): ").then((n) => {
    rl.close();
    return n.trim();
  });
}

/** A short random pause before replying. Cheap insurance against looking obviously automated --
 *  see the plan's go-live notes on why that matters for an unofficial client. */
function humanDelay(): Promise<void> {
  return new Promise((r) => setTimeout(r, 1000 + Math.random() * 2000));
}

// Bridge-side only, so no language state to consult -- same both-languages-on-one-line pattern
// messaging.py already uses for its own transport-level notices (e.g. the rate-limit message).
const BACKEND_DOWN_NOTICE =
  "Sorry, I'm having trouble reaching my brain right now. Please try again in a moment. / माफ़ कीजिए, अभी जवाब नहीं बन पा रहा। थोड़ी देर में फिर कोशिश कीजिए।";

async function forwardToBackend(jid: string, body: string, audioB64: string | null): Promise<void> {
  let messages: string[];
  try {
    const res = await fetch(`${BACKEND_URL}/bridge/whatsapp`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jid, body, audio_b64: audioB64, bridge_url: SELF_URL }),
    });
    if (!res.ok) throw new Error(`backend returned HTTP ${res.status}`);
    const reply = (await res.json()) as { messages?: string[] };
    messages = reply.messages ?? [];
    if (messages.length === 0) throw new Error("backend returned no messages");
  } catch (err) {
    // Never go silent: a judge/user with no reply at all reads as "the bot is broken", which is
    // a worse failure than one honest line saying something went wrong.
    logger.error({ err }, "backend unreachable or returned nothing usable");
    try {
      await currentSocket?.sendMessage(jid, { text: BACKEND_DOWN_NOTICE });
    } catch (sendErr) {
      logger.error({ sendErr }, "could not even send the down-notice");
    }
    return;
  }
  for (const text of messages) {
    await humanDelay();
    await currentSocket?.sendMessage(jid, { text });
  }
}

async function handleIncoming(sock: Socket, m: { message?: unknown; key: { remoteJid?: string | null; fromMe?: boolean | null } }): Promise<void> {
  const jid = m.key.remoteJid;
  if (!m.message || m.key.fromMe || !jid) return;
  if (jid.endsWith("@g.us") || jid === "status@broadcast") return; // this is a 1:1 help line, not a group bot

  const content = m.message as Record<string, unknown>;
  const contentType = getContentType(content as Parameters<typeof getContentType>[0]);

  if (contentType === "audioMessage") {
    try {
      const buf = (await downloadMediaMessage(
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        m as any,
        "buffer",
        {},
        { logger, reuploadRequest: sock.updateMediaMessage },
      )) as Buffer;
      await forwardToBackend(jid, "", buf.toString("base64"));
    } catch (err) {
      logger.error({ err }, "voice note download failed");
    }
    return;
  }

  const text =
    (content.conversation as string | undefined) ||
    (content.extendedTextMessage as { text?: string } | undefined)?.text ||
    "";
  if (!text) return; // an image, sticker, location, etc. with nothing to answer
  await forwardToBackend(jid, text, null);
}

async function connect(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const sock = makeWASocket({ auth: state, logger });
  currentSocket = sock;

  if (!sock.authState.creds.registered) {
    const phoneNumber = PAIR_NUMBER || (await askForNumber());
    // WhatsApp wants the socket to settle before a pairing code is requested.
    await new Promise((r) => setTimeout(r, 3000));
    const code = await sock.requestPairingCode(phoneNumber);
    console.log("=".repeat(44));
    console.log(" Pairing code:", code);
    console.log(" WhatsApp -> Linked Devices -> Link a Device");
    console.log(" -> Link with phone number instead -> enter this code");
    console.log("=".repeat(44));
  }

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect } = update;
    if (connection === "close") {
      const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      if (loggedOut) {
        logger.error("WhatsApp logged this session out -- delete WHATSAPP_AUTH_DIR and re-pair");
      } else {
        logger.warn({ statusCode }, "connection closed, reconnecting");
        // A bare `void connect()` here is what actually took the whole bridge down earlier: a
        // reconnect attempt that itself rejects (a raw WebSocket abnormal-closure, status 1006,
        // observed live) had nowhere to land and crashed the process via an unhandled rejection.
        // One dropped reconnect should never be worse than the connection it was trying to fix.
        setTimeout(() => {
          connect().catch((err) => logger.error({ err }, "reconnect attempt failed, will retry on the next close"));
        }, 2000);
      }
    } else if (connection === "open") {
      logger.info("connected to WhatsApp");
    }
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      handleIncoming(sock, m).catch((err) => logger.error({ err }, "unhandled error processing an incoming message"));
    }
  });
}

startSendServer(() => currentSocket, BRIDGE_PORT, logger);

// Last-resort net: in a judged demo, a stray rejection anywhere taking down the whole bridge
// is strictly worse than logging it and staying up. Everything already has its own try/catch;
// this only catches what that missed.
process.on("unhandledRejection", (reason) => logger.error({ reason }, "unhandled rejection (bridge stayed up)"));
process.on("uncaughtException", (err) => logger.error({ err }, "uncaught exception (bridge stayed up)"));

connect().catch((err) => {
  logger.error({ err }, "fatal");
  process.exit(1);
});
