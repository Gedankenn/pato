IMAGE = pato-backend
CONTAINER = pato
WA_IMAGE = pato-whatsapp
WA_CONTAINER = pato-whatsapp
DB_PATH = /data/pato.db
DATA_VOLUME = /mnt/user/appdata/pato/data
WA_VOLUME = /mnt/user/appdata/pato/wa-data
SERVER = root@tower.local
SERVER_DIR = /mnt/user/appdata/pato

.PHONY: build restart deploy logs ssh sync sync-wa wa-build wa-logs log log-wa log-all

build:
	docker build -t $(IMAGE) .

restart:
	docker rm -f $(CONTAINER) 2>/dev/null; \
	docker run -d --name $(CONTAINER) --network host --restart unless-stopped \
		-e PATO_DB_PATH=$(DB_PATH) \
		-e WHATSAPP_DATA_DIR=/wa-data \
		-v $(DATA_VOLUME):/data \
		-v $(WA_VOLUME):/wa-data \
		-v /mnt/user/appdata/pato/.env:/app/.env \
		$(IMAGE)

deploy: build restart

logs:
	docker logs -f $(CONTAINER)

wa-build:
	cd whatsapp && docker build -t $(WA_IMAGE) .

wa-restart:
	docker rm -f $(WA_CONTAINER) 2>/dev/null; \
	docker run -d --name $(WA_CONTAINER) --network host --restart unless-stopped \
		-e MANAGER_PORT=8001 \
		-e PATO_API_URL=http://localhost:8000 \
		-e CHROMIUM_PATH=/usr/bin/chromium \
		-v $(WA_VOLUME):/app/data \
		$(WA_IMAGE)

wa-deploy: wa-build wa-restart

wa-logs:
	docker logs -f $(WA_CONTAINER)

ssh:
	ssh $(SERVER)

sync:
	rsync -avz --delete \
		--exclude='.git' \
		--exclude='__pycache__' \
		--exclude='*.pyc' \
		--exclude='*.db' \
		--exclude='.env' \
		--exclude='.venv' \
		--exclude='node_modules' \
		--exclude='whatsapp/sessions' \
		--exclude='whatsapp/.wwebjs_cache' \
		--exclude='whatsapp/public' \
		--exclude='whatsapp/node_modules' \
		--exclude='.opencode' \
		/home/sabinho/github/pato/ $(SERVER):$(SERVER_DIR)
	ssh $(SERVER) "cd $(SERVER_DIR) && docker build -t $(IMAGE) . && docker rm -f $(CONTAINER) 2>/dev/null && docker run -d --name $(CONTAINER) --network host --restart unless-stopped -e PATO_DB_PATH=$(DB_PATH) -e WHATSAPP_DATA_DIR=/wa-data -v $(DATA_VOLUME):/data -v $(WA_VOLUME):/wa-data -v $(SERVER_DIR)/.env:/app/.env $(IMAGE)"

sync-wa:
	rsync -avz --delete \
		--exclude='node_modules' \
		--exclude='sessions' \
		--exclude='.wwebjs_cache' \
		--exclude='public' \
		/home/sabinho/github/pato/whatsapp/ $(SERVER):$(SERVER_DIR)/whatsapp/
	ssh $(SERVER) "cd $(SERVER_DIR)/whatsapp && docker build -t $(WA_IMAGE) . && docker rm -f $(WA_CONTAINER) 2>/dev/null && docker run -d --name $(WA_CONTAINER) --network host --restart unless-stopped -e MANAGER_PORT=8001 -e PATO_API_URL=http://localhost:8000 -e CHROMIUM_PATH=/usr/bin/chromium -v $(WA_VOLUME):/app/data $(WA_IMAGE)"

# ── Remote log watching ──────────────────────────────────────
log:
	ssh $(SERVER) "docker logs -f --tail 50 $(CONTAINER)"

log-wa:
	ssh $(SERVER) "docker logs -f --tail 50 $(WA_CONTAINER)"

log-all:
	ssh $(SERVER) "docker logs -f --tail 20 $(CONTAINER) & docker logs -f --tail 20 $(WA_CONTAINER); wait"

# ── WhatsApp session management ──────────────────────────────
wa-stop:
	@read -p "Barbershop ID: " id; \
	ssh $(SERVER) "curl -s -X DELETE http://localhost:8001/manager/stop/$$id"

wa-start:
	@read -p "Barbershop ID: " id; \
	read -sp "Admin password: " pw; echo; \
	TOKEN=$$(ssh $(SERVER) "curl -s -X POST http://localhost:8000/auth/login -H 'Content-Type: application/json' -d '{\"email\":\"fabioslikastella@gmail.com\",\"password\":\"'$$pw'\"}' | grep -o '\"token\":\"[^\"]*\"' | cut -d'\"' -f4"); \
	ssh $(SERVER) "curl -s -X POST -H 'Authorization: Bearer $$TOKEN' http://localhost:8000/admin/start-whatsapp/$$id"