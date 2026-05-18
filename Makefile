IMAGE = pato-backend
CONTAINER = pato
DB_PATH = /data/pato.db
DATA_VOLUME = /mnt/user/appdata/pato/data
SERVER = root@tower.local
SERVER_DIR = /mnt/user/appdata/pato

.PHONY: build restart deploy logs ssh sync

build:
	docker build -t $(IMAGE) .

restart:
	docker rm -f $(CONTAINER) 2>/dev/null; \
	docker run -d --name $(CONTAINER) --network host --restart unless-stopped \
		-e PATO_DB_PATH=$(DB_PATH) \
		-v $(DATA_VOLUME):/data \
		$(IMAGE)

deploy: build restart

logs:
	docker logs -f $(CONTAINER)

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
	ssh $(SERVER) "cd $(SERVER_DIR) && docker build -t $(IMAGE) . && docker rm -f $(CONTAINER) 2>/dev/null && docker run -d --name $(CONTAINER) --network host --restart unless-stopped -e PATO_DB_PATH=$(DB_PATH) -v $(DATA_VOLUME):/data $(IMAGE)"