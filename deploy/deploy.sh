#!/usr/bin/env bash
#
# Deploy Card Reader onto the box that already runs Ollama.
#
#   ssh ubuntu@<host>
#   cd ~/card-reader && ./deploy/deploy.sh
#
# Idempotent: safe to re-run after a git pull. That matters more than it
# sounds -- a deploy script you are afraid to run twice is one you end up
# running by hand in pieces, which is how a box drifts from its own repo.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------
log "Checking Ollama is up and has the model"
# Checked FIRST and loudly. Every other step can succeed while the app is
# useless if this is wrong, and "the model is missing" surfaces much later as
# an opaque per-card model_error.
if ! systemctl is-active --quiet ollama; then
    echo "ollama is not running. Start it with: sudo systemctl start ollama" >&2
    exit 1
fi
if ! ollama list | grep -q "qwen2.5vl:3b"; then
    echo "qwen2.5vl:3b is not pulled. Run: ollama pull qwen2.5vl:3b" >&2
    exit 1
fi
echo "ollama active, qwen2.5vl:3b present"

# --------------------------------------------------------------------------
log "Keeping the model resident (OLLAMA_KEEP_ALIVE=-1)"
# Ollama unloads an idle model after 5 minutes by default. The next card then
# pays a 3.2 GB reload on top of inference. With cards arriving in bursts
# minutes apart, that reload is paid over and over for no reason -- the box has
# 7.6 GiB and nothing else wants the RAM.
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/keepalive.conf >/dev/null <<'CONF'
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
CONF
sudo systemctl daemon-reload
sudo systemctl restart ollama
echo "keep-alive set; model stays resident"

# --------------------------------------------------------------------------
log "Python environment"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv nginx >/dev/null
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
echo "dependencies installed"

# --------------------------------------------------------------------------
log "Runtime configuration"
# Written only if absent, so a hand-edited .env on the box survives a redeploy.
if [ ! -f .env ]; then
    cat > .env <<'ENVFILE'
MODEL_URL=http://127.0.0.1:11434/v1/chat/completions
MODEL_NAME=qwen2.5vl:3b
MODEL_TIMEOUT_SECONDS=600
MODEL_MAX_ATTEMPTS=2
STORE_BACKEND=sqlite
DB_PATH=/var/lib/card-reader/leads.db
IMAGE_DIR=/var/lib/card-reader/images
IMAGE_RETENTION_DAYS=7
RECLAIM_STALE_JOBS=true
MAX_FILES_PER_REQUEST=20
MAX_CONCURRENCY=1
AUTH_MODE=local
ENVFILE
    echo "wrote .env"
else
    echo ".env already present, left alone"
fi

sudo mkdir -p /var/lib/card-reader
sudo chown -R "$USER":"$USER" /var/lib/card-reader

# --------------------------------------------------------------------------
log "systemd service"
sudo cp deploy/card-reader.service /etc/systemd/system/card-reader.service
# The unit ships with a generic User=cardreader; on this box the checkout and
# the venv belong to the login user, so point it at them rather than creating a
# second user and a second copy of everything.
sudo sed -i "s|^User=.*|User=$USER|; s|^Group=.*|Group=$USER|; \
             s|^WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|; \
             s|^ExecStart=.*|ExecStart=$REPO_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1|; \
             s|^EnvironmentFile=.*|EnvironmentFile=-$REPO_DIR/.env|" \
    /etc/systemd/system/card-reader.service
# ProtectHome would hide /home/ubuntu, where the checkout lives.
sudo sed -i "s|^ProtectHome=true|ProtectHome=false|" /etc/systemd/system/card-reader.service
sudo systemctl daemon-reload
sudo systemctl enable --now card-reader
sudo systemctl restart card-reader
echo "card-reader service started"

# --------------------------------------------------------------------------
log "nginx on :80"
sudo cp deploy/nginx-card-reader.conf /etc/nginx/sites-available/card-reader
sudo ln -sf /etc/nginx/sites-available/card-reader /etc/nginx/sites-enabled/card-reader
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx
echo "nginx proxying :80 -> :8000 with a 900s read timeout"

# --------------------------------------------------------------------------
log "Verifying"
for i in $(seq 1 30); do
    curl -fsS http://127.0.0.1/health >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS http://127.0.0.1/health | python3 -m json.tool
echo
echo "Deployed. http://$(curl -s --max-time 5 ifconfig.me || echo '<public-ip>')/"
