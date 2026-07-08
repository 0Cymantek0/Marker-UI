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
mkdir -p data/uploads data/output data/logs
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

get_int_env() {
    local name=$1
    local default=$2
    local minimum=$3
    local raw="${!name:-}"

    if [ -z "$raw" ]; then
        echo "$default"
        return
    fi

    if [[ "$raw" =~ ^[0-9]+$ ]] && [ "$raw" -ge "$minimum" ]; then
        echo "$raw"
        return
    fi

    warn "Ignoring invalid $name='$raw'; using $default." >&2
    echo "$default"
}

http_ready() {
    local url=$1

    if command -v curl &>/dev/null; then
        curl -fsS --max-time 2 "$url" >/dev/null 2>&1
        return $?
    fi

    if command -v wget &>/dev/null; then
        wget -q --timeout=2 -O /dev/null "$url" >/dev/null 2>&1
        return $?
    fi

    "$PYTHON" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

wait_service_ready() {
    local name=$1
    local url=$2
    local pid=$3
    local soft_timeout=$4
    local hard_timeout=$5
    local start=$SECONDS
    local warned=0
    local next_progress=15

    while true; do
        if ! kill -0 "$pid" 2>/dev/null; then
            err "ERROR: $name exited before it became ready."
            return 1
        fi

        if http_ready "$url"; then
            return 0
        fi

        local elapsed=$((SECONDS - start))
        if [ "$elapsed" -ge "$next_progress" ]; then
            info "Still waiting for $name ($elapsed seconds)..."
            next_progress=$((next_progress + 15))
        fi

        if [ "$warned" -eq 0 ] && [ "$elapsed" -ge "$soft_timeout" ]; then
            warn "WARNING: $name is still starting after $soft_timeout seconds."
            warn "Continuing to wait because the process is still running. Press Ctrl+C to stop."
            warned=1
        fi

        if [ "$hard_timeout" -gt 0 ] && [ "$elapsed" -ge "$hard_timeout" ]; then
            err "ERROR: $name did not become ready within hard timeout $hard_timeout seconds."
            return 1
        fi

        sleep 1
    done
}

BACKEND_PORT=$(find_free_port 8000)

if [ "$BACKEND_PORT" -ne 8000 ]; then
    warn "Port 8000 is in use, using port $BACKEND_PORT instead."
fi

export BACKEND_PORT
BACKEND_HOST="127.0.0.1"
FRONTEND_HOST="127.0.0.1"

FRONTEND_PORT=$(find_free_port 5173)

if [ "$FRONTEND_PORT" -ne 5173 ]; then
    warn "Port 5173 is in use, using port $FRONTEND_PORT instead."
fi

# Backend
info "Starting backend on http://$BACKEND_HOST:$BACKEND_PORT ..."
uvicorn app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --app-dir backend &
BACKEND_PID=$!

BACKEND_READY_TIMEOUT_SECONDS=$(get_int_env MARKER_BACKEND_READY_TIMEOUT_SECONDS 120 1)
BACKEND_READY_HARD_TIMEOUT_SECONDS=$(get_int_env MARKER_BACKEND_READY_HARD_TIMEOUT_SECONDS 0 0)
info "Waiting for backend health check (soft timeout $BACKEND_READY_TIMEOUT_SECONDS seconds)..."
if ! wait_service_ready "backend" "http://$BACKEND_HOST:$BACKEND_PORT/api/health" "$BACKEND_PID" "$BACKEND_READY_TIMEOUT_SECONDS" "$BACKEND_READY_HARD_TIMEOUT_SECONDS"; then
    exit 1
fi
ok "Backend health check passed on port $BACKEND_PORT."

# Frontend
info "Starting frontend on http://$FRONTEND_HOST:$FRONTEND_PORT ..."
(cd frontend && BACKEND_PORT=$BACKEND_PORT npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT") > data/logs/frontend.out.log 2> data/logs/frontend.err.log &
FRONTEND_PID=$!

FRONTEND_READY_TIMEOUT_SECONDS=$(get_int_env MARKER_FRONTEND_READY_TIMEOUT_SECONDS 60 1)
FRONTEND_READY_HARD_TIMEOUT_SECONDS=$(get_int_env MARKER_FRONTEND_READY_HARD_TIMEOUT_SECONDS 180 0)
info "Waiting for frontend server (soft timeout $FRONTEND_READY_TIMEOUT_SECONDS seconds)..."
if ! wait_service_ready "frontend" "http://$FRONTEND_HOST:$FRONTEND_PORT/" "$FRONTEND_PID" "$FRONTEND_READY_TIMEOUT_SECONDS" "$FRONTEND_READY_HARD_TIMEOUT_SECONDS"; then
    if [ -f data/logs/frontend.out.log ] || [ -f data/logs/frontend.err.log ]; then
        warn "Last frontend log lines:"
        [ -f data/logs/frontend.out.log ] && tail -n 20 data/logs/frontend.out.log
        [ -f data/logs/frontend.err.log ] && tail -n 20 data/logs/frontend.err.log
    fi
    exit 1
fi
ok "Frontend server ready on port $FRONTEND_PORT."

# ----------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------
echo ""
ok "========================================================"
ok "Marker UI is running!"
echo ""
info "  Frontend:  ${CYAN}http://$FRONTEND_HOST:$FRONTEND_PORT${NC}"
info "  Backend:   ${CYAN}http://$BACKEND_HOST:$BACKEND_PORT${NC}"
info "  API Docs:  ${CYAN}http://$BACKEND_HOST:$BACKEND_PORT/docs${NC}"
echo ""
warn "  Press Ctrl+C to stop both services."
ok "========================================================"
echo ""

# Wait
wait
