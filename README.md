# PatoAgenda AI

AI-powered appointment scheduling with WhatsApp integration. Built with FastAPI, Ollama / OpenAI-compatible LLMs, and SQLite.

## Features

- **WhatsApp Bot** — customers book, cancel, and reschedule appointments via WhatsApp
- **LLM Receptionist** — natural conversation flow, asks for name, service, time, and preferred staff member
- **Weekly Calendar** — color-coded appointment blocks with drag-free navigation
- **Staff Management** — assign staff members to appointments, enforced in the bot flow
- **Service Management** — configure services with name, duration, and price
- **Automatic Reminders** — WhatsApp reminders sent daily at 5 PM (one day before)
- **Business Type** — 19 business types supported (barbershop, salon, spa, tattoo, etc.)
- **Reports** — 30-day stats: appointments per day, popular services, revenue
- **Admin Panel** — manage all barbershops from a single dashboard
- **Multi-tenancy** — each barbershop has its own services, staff, and appointments

## Architecture

```
┌─────────────┐     HTTP/chat     ┌──────────────┐     WhatsApp Web     ┌────────────────┐
│  WhatsApp   │ ◄──────────────► │  pato-backend │ ◄──────────────────► │ pato-whatsapp  │
│   Client    │                   │  (FastAPI)   │                       │  (Node.js)     │
└─────────────┘                   │  Port 8000   │                       │  Port 8001     │
                                  └──────┬───────┘                       └────────────────┘
                                         │
                                         ▼
                                  ┌──────────────┐
                                  │   Ollama /   │
                                  │ OpenAI API   │
                                  └──────────────┘
```

- **pato-backend**: Python/FastAPI API server with LLM integration
- **pato-whatsapp**: Node.js service running `whatsapp-web.js` to bridge WhatsApp and the API
- **SQLite**: single-file database (`pato.db`), zero configuration

## Quick Start

### Prerequisites

- Docker and Docker Compose (or Docker Engine)
- An LLM provider (Ollama, OpenAI, or any OpenAI-compatible API)
- (Optional) A server for production deployment

### 1. Clone & Configure

```bash
git clone https://github.com/your-org/pato.git
cd pato
cp .env.example .env
```

Edit `.env` with your LLM settings:

```env
# LLM (Ollama example)
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama
LLM_MODEL=qwen2.5-coder:14b

# API URL (used by the WhatsApp bot to reach the backend)
PATO_API_URL=http://localhost:8000

# Database path inside the container
PATO_DB_PATH=/data/pato.db
```

### 2. Run the Backend

```bash
docker build -t pato-backend .
docker run -d --name pato --network host --restart unless-stopped \
  -e PATO_DB_PATH=/data/pato.db \
  -e WHATSAPP_DATA_DIR=/wa-data \
  -v $(pwd)/data:/data \
  -v $(pwd)/wa-data:/wa-data \
  -v $(pwd)/.env:/app/.env \
  pato-backend
```

The backend will be available at `http://localhost:8000`.

### 3. Run the WhatsApp Bot (optional)

```bash
cd whatsapp
docker build -t pato-whatsapp .
docker run -d --name pato-whatsapp --network host --restart unless-stopped \
  -e MANAGER_PORT=8001 \
  -e PATO_API_URL=http://localhost:8000 \
  -e CHROMIUM_PATH=/usr/bin/chromium \
  -v $(pwd)/data:/app/data \
  pato-whatsapp
```

### 4. Access the Web UI

Open `http://localhost:8000/login` and register your first barbershop.

## Using the Makefile

The `Makefile` simplifies local development and remote deployment:

| Command | Description |
|---------|-------------|
| `make build` | Build the backend Docker image |
| `make restart` | Stop and restart the backend container |
| `make deploy` | Build + restart (local) |
| `make logs` | Tail backend container logs |
| `make wa-build` | Build the WhatsApp bot image |
| `make wa-restart` | Restart the WhatsApp bot container |
| `make wa-logs` | Tail WhatsApp bot logs |
| `make sync` | Rsync code to remote server + rebuild + restart |
| `make sync-wa` | Rsync WhatsApp bot code to remote server + rebuild |
| `make ssh` | SSH into the remote server |

**To customize the Makefile** for your own server, edit these variables at the top:

```makefile
SERVER = root@your-server.local
SERVER_DIR = /path/on/server/pato
DATA_VOLUME = /path/on/server/data
WA_VOLUME = /path/on/server/wa-data
```

## Environment Variables

### pato-backend

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | LLM API endpoint (Ollama, OpenAI, etc.) |
| `OPENAI_API_KEY` | `ollama` | API key for the LLM provider |
| `LLM_MODEL` | `qwen2.5-coder:14b` | Model name to use |
| `PATO_DB_PATH` | `pato.db` | Path to the SQLite database |
| `WHATAPP_DATA_DIR` | `/wa-data` | WhatsApp session data directory |
| `ADMIN_EMAIL` | `admin@patoagenda.com` | Default admin email (first run) |
| `ADMIN_PASSWORD` | `admin123` | Default admin password (first run) |

### pato-whatsapp

| Variable | Default | Description |
|----------|---------|-------------|
| `MANAGER_PORT` | `8001` | WhatsApp manager HTTP port |
| `PATO_API_URL` | `http://localhost:8000` | Backend API URL |
| `CHROMIUM_PATH` | `/usr/bin/chromium` | Path to Chromium binary |

## API Endpoints

### Auth
- `POST /auth/register` — Create a new barbershop
- `POST /auth/login` — Login, returns JWT token

### Appointments (scoped to barbershop)
- `GET /appointments` — List appointments
- `POST /appointments` — Create appointment
- `GET /appointments/{id}` — Get appointment details
- `DELETE /appointments/{id}` — Cancel appointment
- `PUT /appointments/{id}/reschedule` — Reschedule appointment

### Services (scoped)
- `GET /services` — List services
- `POST /services` — Create service
- `PUT /services/{id}` — Update service
- `DELETE /services/{id}` — Delete service

### Staff (scoped)
- `GET /staff` — List staff members
- `POST /staff` — Create staff member
- `PUT /staff/{id}` — Update staff member
- `DELETE /staff/{id}` — Delete staff member

### Chat
- `POST /chat` — Send message to the LLM receptionist

### WhatsApp
- `GET /whatsapp/qrcode` — Get QR code for WhatsApp Web pairing

### Admin (admin-only)
- `GET /admin` — Admin dashboard
- `GET /admin/barbershops` — List all barbershops
- `DELETE /admin/barbershops/{id}` — Delete a barbershop
- `GET /admin/appointments` — List all appointments
- `GET /admin/stats` — Global stats

## Web Pages

| Path | Description |
|------|-------------|
| `/login` | Login / Register |
| `/dashboard` | Weekly calendar with filters |
| `/config` | Services, staff, business type, WhatsApp QR |
| `/reports` | 30-day statistics |
| `/admin` | Admin panel (multi-shop management) |
| `/demo` | Auto-login demo (PatoBarba) |

## Project Structure

```
pato/
├── app/
│   ├── main.py          # FastAPI app, all endpoints, HTML templates
│   ├── database.py      # SQLite schema, migrations, CRUD operations
│   ├── schemas.py       # Pydantic models
│   ├── auth.py          # JWT authentication utilities
│   └── llm.py           # System prompt builder
├── whatsapp/
│   ├── manager.js       # WhatsApp session manager
│   ├── bot.js           # WhatsApp message handler
│   ├── package.json     # Node.js dependencies
│   └── Dockerfile       # WhatsApp bot Docker image
├── static/
│   └── logo.png         # App logo
├── requirements.txt     # Python dependencies
├── Dockerfile           # Backend Docker image
├── Makefile             # Build & deploy shortcuts
├── .env.example         # Environment variables template
└── README.md
```

## Troubleshooting

**"Calendar not showing"** — Hard refresh the browser (Ctrl+Shift+R). The calendar is rendered client-side; a cached old script will break it.

**LLM returns wrong times** — The system prompt uses a clearly marked example date (`2025-01-01`). If the LLM copies it, the backend rejects it and asks the LLM to use the correct date.

**WhatsApp QR not scanning** — Make sure the WhatsApp container can reach the backend at `PATO_API_URL`. Check logs with `make wa-logs`.

**Database locked** — SQLite uses WAL mode. Ensure the data directory has proper permissions and only one process writes to it.
