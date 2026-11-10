#!/bin/sh
# Backup diário do SQLite do Patinho (consistente, via API do sqlite3)
set -e
SRC=/home/server/docker/patinho/data/patinho.db
DST=/home/server/backups/patinho
mkdir -p "$DST"
python3 - "$SRC" "$DST/patinho-$(date +%Y%m%d-%H%M).db" <<"PY"
import sqlite3, sys, os
src, dst = sys.argv[1], sys.argv[2]
if not os.path.exists(src):
    sys.exit(0)
a = sqlite3.connect(src); b = sqlite3.connect(dst)
a.backup(b); b.close(); a.close()
PY
find "$DST" -name "patinho-*.db" -mtime +7 -delete
