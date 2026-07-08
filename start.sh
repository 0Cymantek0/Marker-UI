#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Marker UI - One-click launcher (Linux / macOS)
# Usage: chmod +x start.sh && ./start.sh
# ----------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "      Marker UI - One-Click Launcher"
echo "  ========================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "  $1"; }
ok()    { echo -e "  ${GREEN}$1${NC}"; }
warn()  { echo -e "  ${YELLOW}$1${NC}"; }
err()   { echo -e "  ${RED}$1${NC}"; }

# ----------------------------------------------------------------------
# Prerequisites
# ----------------------------------------------------------------------
echo -e "${YELLOW}[1/6] Checking prerequisites...${NC}"

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" &>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "ERROR: Python 3.10+ not found. Install from https://python.org"
    exit 1
fi
ok "Python: $($PYTHON --version 2>&1)"

if ! command -v node &>/dev/null; then
    err "ERROR: Node.js not found. Install from https://nodejs.org"
    exit 1
fi
ok "Node.js: $(node --version)"

# ----------------------------------------------------------------------
# Virtual environment
# ----------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[2/6] Setting up Python virtual environment...${NC}"

if [ ! -d ".venv" ]; then
    info "Creating .venv..."
    $PYTHON -m venv .venv
    if [ $? -ne 0 ]; then
        err "Failed to create virtual environment"
        exit 1
    fi
fi

# Activate
source .venv/bin/activate
ok "Virtual environment ready"

# ----------------------------------------------------------------------
# Install Python deps
# ----------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[3/6] Installing Python dependencies...${NC}"

if [ ! -f ".venv/installed" ]; then
    info "Installing dependencies (first run may take a while)..."
    if pip install -r backend/requirements.txt --quiet; then
        touch .venv/installed
        ok "Python dependencies installed"
    else
        warn "Full install had issues, retrying without [full] extra..."
        if grep -v "marker-pdf\[full\]" backend/requirements.txt | pip install -r /dev/stdin --quiet && pip install marker-pdf --quiet; then
            touch .venv/installed
            ok "Python dependencies installed"
        else
            err "ERROR: Python dependency installation failed."
            exit 1
        fi
    fi
else
    info "Python dependencies already installed, skipping check."
fi

# ----------------------------------------------------------------------
# Install Node deps
# ----------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[4/6] Installing Node.js dependencies...${NC}"

cd frontend
if [ ! -d "node_modules" ]; then
    npm install --loglevel error
    if [ $? -ne 0 ]; then
        err "npm install failed"
        cd ..
        exit 1
    fi
else
    info "node_modules exists, skipping install"
fi
cd ..
ok "Node.js dependencies installed"

# ----------------------------------------------------------------------
# Data dirs
# ----------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[5/6] Creating data directories...${NC}"
mkdir -p data/uploads data/output
ok "Data directories ready"

# ----------------------------------------------------------------------
# Start services
# ----------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[6/6] Starting services...${NC}"
echo ""

kill_tree() {
    local pid="${1:-}"
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || return 0

    if command -v pgrep &>/dev/null; then
        local child
        for child in $(pgrep -P "$pid" 2>/dev/null || true); do
            kill_tree "$child"
        done
    fi

    kill "$pid" 2>/dev/null || true
}

cleanup() {
    local exit_code=$?
    trap - EXIT SIGINT SIGTERM
    echo ""
    warn "Stopping services..."
    kill_tree "${FRONTEND_PID:-}"
    kill_tree "${BACKEND_PID:-}"
    ok "Services stopped."
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' SIGINT
trap 'exit 143' SIGTERM

port_in_use() {
    local port=$1
    if command -v lsof &>/dev/null; then
        lsof -ti:$port &>/dev/null
    elif command -v ss &>/dev/null; then
        ss -tln | grep -E -q ":$port( |$)"
    elif command -v netstat &>/dev/null; then
        netstat -tln | grep -E -q ":$port( |$)"
    else
        # Fallback to bash built-in /dev/tcp
        (echo > /dev/tcp/127.0.0.1/$port) &>/dev/null
    fi
}

find_free_port() {
    local port=$1
    while port_in_use "$port"; do
        port=$((port + 1))
        if [ $port -ge 65535 ]; then
            err "No free port found."
            exit 1
        fi
    done
    echo $port
}

BACKEND_PORT=$(find_free_port 8000)

if [ "$BACKEND_PORT" -ne 8000 ]; then
    warn "Port 8000 is in use, using port $BACKEND_PORT instead."
fi

export BACKEND_PORT

FRONTEND_PORT=$(find_free_port 5173)

if [ "$FRONTEND_PORT" -ne 5173 ]; then
    warn "Port 5173 is in use, using port $FRONTEND_PORT instead."
fi

# Backend
info "Starting backend on http://localhost:$BACKEND_PORT ..."
uvicorn app.main:app --host 0.0.0.0 --port $BACKEND_PORT --app-dir backend &
BACKEND_PID=$!
sleep 3

if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    err "Backend failed to start."
    exit 1
fi

# Frontend
info "Starting frontend on http://localhost:$FRONTEND_PORT ..."
cd frontend && BACKEND_PORT=$BACKEND_PORT npm run dev -- --port $FRONTEND_PORT > /dev/null 2>&1 &
FRONTEND_PID=$!
cd ..
sleep 3

# ----------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------
echo ""
ok "========================================================"
ok "Marker UI is running!"
echo ""
info "  Frontend:  ${CYAN}http://localhost:$FRONTEND_PORT${NC}"
info "  Backend:   ${CYAN}http://localhost:$BACKEND_PORT${NC}"
info "  API Docs:  ${CYAN}http://localhost:$BACKEND_PORT/docs${NC}"
echo ""
warn "  Press Ctrl+C to stop both services."
ok "========================================================"
echo ""

# Wait
wait
