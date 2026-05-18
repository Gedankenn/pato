const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const QRCode = require("qrcode");
const axios = require("axios");
const fs = require("fs");
const path = require("path");

const API_URL = process.env.PATO_API_URL || "http://localhost:8000";
const SESSION_DIR = path.join(__dirname, "sessions");
const PUBLIC_DIR = path.join(__dirname, "public");
const QR_IMAGE = path.join(PUBLIC_DIR, "qrcode.png");
const STATUS_FILE = path.join(PUBLIC_DIR, "status.json");

for (const dir of [SESSION_DIR, PUBLIC_DIR]) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

const CHROMIUM_PATH =
  process.env.CHROMIUM_PATH ||
  "/home/sabinho/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome";

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
  puppeteer: {
    headless: true,
    executablePath: CHROMIUM_PATH,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
    ],
  },
});

// ── Eventos do WhatsApp ──────────────────────────────────────────

client.on("qr", (qr) => {
  console.clear();
  console.log("╔════════════════════════════════════════════╗");
  console.log("║    PATO - Assistente de Agendamento        ║");
  console.log("╠════════════════════════════════════════════╣");
  console.log("║  Escaneie o QR Code abaixo com o           ║");
  console.log("║  WhatsApp do celular da barbearia:         ║");
  console.log("║                                            ║");
  console.log("║  WhatsApp > Três pontos > Aparelhos        ║");
  console.log("║  conectados > Conectar um dispositivo      ║");
  console.log("╚════════════════════════════════════════════╝");
  console.log("");
  qrcode.generate(qr, { small: true });

  // Salvar QR como imagem para exibir no dashboard
  QRCode.toFile(QR_IMAGE, qr, { type: "png", width: 400 }, (err) => {
    if (err) console.error("Erro ao salvar QR:", err.message);
  });

  // Salvar status
  fs.writeFileSync(
    STATUS_FILE,
    JSON.stringify({ status: "awaiting_scan", qr_updated: new Date().toISOString() })
  );
});

client.on("loading_screen", (percent, message) => {
  console.log(`Carregando: ${percent}% - ${message}`);
});

client.on("authenticated", () => {
  console.log("✅ WhatsApp autenticado com sucesso!");
  fs.writeFileSync(
    STATUS_FILE,
    JSON.stringify({ status: "authenticated", qr_updated: null })
  );
});

client.on("auth_failure", (msg) => {
  console.error("❌ Falha na autenticação:", msg);
  fs.writeFileSync(
    STATUS_FILE,
    JSON.stringify({ status: "auth_failure", error: msg })
  );
});

client.on("ready", () => {
  console.log("🤖 Pato está pronto! Aguardando mensagens...");
  console.log(`📡 Conectado ao servidor: ${API_URL}`);
  console.log("");
  fs.writeFileSync(
    STATUS_FILE,
    JSON.stringify({ status: "connected", qr_updated: null })
  );
});

client.on("disconnected", (reason) => {
  console.log("❌ Desconectado:", reason);
  fs.writeFileSync(
    STATUS_FILE,
    JSON.stringify({ status: "disconnected", reason })
  );
});

// ── Processar mensagens recebidas ──────────────────────────────

client.on("message", async (message) => {
  try {
    // Ignorar mensagens de grupos e status
    if (message.from.includes("@g.us") || message.from.includes("@broadcast")) {
      return;
    }

    // Ignorar mensagens do próprio bot (para evitar loops)
    if (message.fromMe) return;

    const contact = await message.getContact();
    const customerName =
      contact.pushname || contact.name || contact.number || "Cliente";
    const phoneNumber = message.from.replace("@c.us", "");

    console.log(`\n📩 Mensagem de ${customerName} (${phoneNumber}):`);
    console.log(`   ${message.body}`);

    // Enviar indicador de digitação
    await client.sendPresenceAvailable();
    await message.reply("⏱️ Um momento, estou verificando...");

    // Enviar para o servidor Pato
    const response = await axios.post(
      `${API_URL}/chat`,
      {
        message: message.body,
        thread_id: message.from,
        customer_name: customerName,
      },
      { timeout: 300000 }
    );

    const reply = response.data.reply;

    if (reply && reply.trim()) {
      await message.reply(reply);
      console.log(`✅ Resposta enviada para ${customerName}`);
    }
  } catch (error) {
    if (error.code === "ECONNREFUSED") {
      console.error("❌ Servidor Pato não está rodando!");
      await message.reply(
        "🛠️ Desculpe, estou tendo problemas técnicos. Tente novamente em alguns minutos."
      );
    } else if (error.response) {
      console.error("❌ Erro do servidor:", error.response.status);
      await message.reply(
        "❌ Ocorreu um erro ao processar sua mensagem. Pode repetir?"
      );
    } else {
      console.error("❌ Erro:", error.message);
    }
  }
});

// ── Iniciar ────────────────────────────────────────────────────

console.log("🚀 Iniciando Pato WhatsApp Bot...");
client.initialize();
