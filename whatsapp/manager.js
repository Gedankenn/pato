const { Client, LocalAuth } = require("whatsapp-web.js");
const QRCode = require("qrcode");
const axios = require("axios");
const express = require("express");
const fs = require("fs");
const path = require("path");

const PORT = process.env.MANAGER_PORT || 8001;
const API_URL = process.env.PATO_API_URL || "http://localhost:8000";
const DATA_DIR = path.join(__dirname, "data");

// Map of barbershop_id -> WhatsApp Client
const sessions = new Map();

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

const CHROMIUM_PATH =
  process.env.CHROMIUM_PATH ||
  "/home/sabinho/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome";

// ── Session Management ──────────────────────────────────────────

function getSessionDir(barbershopId) {
  const dir = path.join(DATA_DIR, String(barbershopId));
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function writeStatus(barbershopId, status) {
  const file = path.join(getSessionDir(barbershopId), "status.json");
  fs.writeFileSync(file, JSON.stringify({ ...status, updated_at: new Date().toISOString() }));
}

async function startSession(barbershopId) {
  if (sessions.has(barbershopId)) {
    console.log(`[${barbershopId}] Session already running`);
    return;
  }

  const dir = getSessionDir(barbershopId);
  console.log(`[${barbershopId}] Starting WhatsApp session...`);

  const client = new Client({
    authStrategy: new LocalAuth({
      dataPath: path.join(dir, "session"),
    }),
    puppeteer: {
      headless: true,
      executablePath: CHROMIUM_PATH,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    },
  });

  client.on("qr", async (qr) => {
    console.log(`[${barbershopId}] QR code generated`);
    writeStatus(barbershopId, { status: "awaiting_scan" });
    try {
      await QRCode.toFile(path.join(dir, "qrcode.png"), qr, { type: "png", width: 400 });
    } catch (e) {
      console.error(`[${barbershopId}] QR save error:`, e.message);
    }
  });

  client.on("authenticated", () => {
    console.log(`[${barbershopId}] Authenticated`);
    writeStatus(barbershopId, { status: "authenticated" });
  });

  client.on("ready", () => {
    console.log(`[${barbershopId}] Ready`);
    writeStatus(barbershopId, { status: "connected" });
  });

  client.on("disconnected", (reason) => {
    console.log(`[${barbershopId}] Disconnected:`, reason);
    writeStatus(barbershopId, { status: "disconnected", reason });
    sessions.delete(barbershopId);
  });

  client.on("auth_failure", (msg) => {
    console.error(`[${barbershopId}] Auth failure:`, msg);
    writeStatus(barbershopId, { status: "auth_failure", error: msg });
  });

  client.on("message", async (message) => {
    try {
      if (message.from.includes("@g.us") || message.from.includes("@broadcast")) return;
      if (message.fromMe) return;

      const contact = await message.getContact();
      const name = contact.pushname || contact.name || "Cliente";
      const phone = message.from.replace("@c.us", "");

      console.log(`[${barbershopId}] 📩 ${name}: ${message.body}`);

      const response = await axios.post(`${API_URL}/webhook/wa-message`, {
        from: phone,
        text: message.body,
        barbershop_id: barbershopId,
        customer_name: name,
      }, { timeout: 300000 });

      const reply = response.data.reply;
      if (reply && reply.trim()) {
        await message.reply(reply);
        console.log(`[${barbershopId}] ✅ Reply sent to ${name}`);
      }
    } catch (err) {
      console.error(`[${barbershopId}] Message error:`, err.message);
    }
  });

  sessions.set(barbershopId, client);
  client.initialize();
}

function stopSession(barbershopId) {
  const client = sessions.get(barbershopId);
  if (client) {
    client.destroy();
    sessions.delete(barbershopId);
    writeStatus(barbershopId, { status: "stopped" });
    console.log(`[${barbershopId}] Session stopped`);
  }
}

function getSessionStatus(barbershopId) {
  const file = path.join(getSessionDir(barbershopId), "status.json");
  if (fs.existsSync(file)) {
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  }
  return { status: "inactive" };
}

// ── HTTP API ────────────────────────────────────────────────────

const app = express();
app.use(express.json());

app.get("/manager/sessions", (req, res) => {
  const result = {};
  if (!fs.existsSync(DATA_DIR)) return res.json(result);
  const dirs = fs.readdirSync(DATA_DIR);
  for (const dir of dirs) {
    if (fs.statSync(path.join(DATA_DIR, dir)).isDirectory()) {
      result[dir] = getSessionStatus(dir);
    }
  }
  res.json(result);
});

app.post("/manager/start", (req, res) => {
  const { barbershop_id } = req.body;
  if (!barbershop_id) return res.status(400).json({ error: "barbershop_id required" });
  startSession(barbershop_id);
  res.json({ status: "starting", barbershop_id });
});

app.get("/manager/status/:id", (req, res) => {
  res.json(getSessionStatus(req.params.id));
});

app.delete("/manager/stop/:id", (req, res) => {
  stopSession(req.params.id);
  res.json({ status: "stopped" });
});

app.post("/manager/send", (req, res) => {
  const { barbershop_id, to, text } = req.body;
  if (!barbershop_id || !to || !text) return res.status(400).json({ error: "barbershop_id, to, and text required" });
  const client = sessions.get(barbershop_id);
  if (!client) return res.status(404).json({ error: "Session not started for this barbershop" });
  const number = to.includes("@c.us") ? to : `${to}@c.us`;
  client.sendMessage(number, text).then(() => {
    console.log(`[${barbershop_id}] Proactive message sent to ${to}`);
    res.json({ status: "sent", to });
  }).catch((err) => {
    console.error(`[${barbershop_id}] Send error:`, err.message);
    res.status(500).json({ error: err.message });
  });
});

// ── Start ───────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`🤖 PatoAgenda AI — WhatsApp Manager rodando na porta ${PORT}`);
  console.log(`📡 Backend: ${API_URL}`);

  // Auto-start sessions for existing barbershops
  if (fs.existsSync(DATA_DIR)) {
    const dirs = fs.readdirSync(DATA_DIR);
    for (const dir of dirs) {
      const statPath = path.join(DATA_DIR, dir, "status.json");
      if (fs.existsSync(statPath)) {
        const status = JSON.parse(fs.readFileSync(statPath, "utf-8"));
        if (status.status === "connected" || status.status === "awaiting_scan" || status.status === "authenticated") {
          console.log(`[${dir}] Auto-starting session...`);
          startSession(parseInt(dir));
        }
      }
    }
  }
});
