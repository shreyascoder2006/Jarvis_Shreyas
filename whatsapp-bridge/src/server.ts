/** The half of the bridge Python talks to, not WhatsApp.
 *
 * /bridge/whatsapp in the Python backend can answer a slow (model-routed) question
 * asynchronously: it replies to the inbound POST with just an acknowledgement, then -- once
 * the real answer is ready -- posts it here so it can go out over the live WhatsApp
 * connection. This endpoint is never reachable from the public internet, only from the
 * Python container on the same Docker network (or localhost in dev), so it carries no auth
 * of its own.
 */
import express from "express";
import type { Logger } from "pino";
import type makeWASocket from "@whiskeysockets/baileys";

type Socket = ReturnType<typeof makeWASocket>;

export function startSendServer(getSocket: () => Socket | null, port: number, logger: Logger): void {
  const app = express();
  app.use(express.json());

  app.post("/send", async (req, res) => {
    const { jid, text } = req.body as { jid?: string; text?: string };
    const sock = getSocket();
    if (!jid || !text) {
      res.status(400).json({ ok: false, reason: "jid and text are required" });
      return;
    }
    if (!sock) {
      res.status(503).json({ ok: false, reason: "not connected to WhatsApp yet" });
      return;
    }
    try {
      await sock.sendMessage(jid, { text });
      res.json({ ok: true });
    } catch (err) {
      logger.error({ err }, "deferred send failed");
      res.status(500).json({ ok: false, reason: "send failed" });
    }
  });

  app.get("/health", (_req, res) => res.json({ ok: true, connected: !!getSocket() }));

  app.listen(port, () => logger.info(`bridge send-server listening on :${port}`));
}
