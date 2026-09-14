#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Port configuration for single-port deployment.
# Platforms like Render inject PORT (default 10000) and route public traffic
# to it, so the public frontend listens on PORT while the API stays internal.
FRONTEND_PORT="${PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
if [ "${FRONTEND_PORT}" = "${BACKEND_PORT}" ]; then
    BACKEND_PORT="8001"
fi
# Tell the Next.js rewrite proxy where the internal API lives.
export BACKEND_ORIGIN="http://127.0.0.1:${BACKEND_PORT}"

# Print banner
print_banner() {
    echo -e "${CYAN}"
    cat << 'EOF'

 ██████╗ ███████╗███████╗██╗   ██╗███╗   ███╗███████╗
 ██╔══██╗██╔════╝██╔════╝██║   ██║████╗ ████║██╔════╝
 ██████╔╝█████╗  ███████╗██║   ██║██╔████╔██║█████╗
 ██╔══██╗██╔══╝  ╚════██║██║   ██║██║╚██╔╝██║██╔══╝
 ██║  ██║███████╗███████║╚██████╔╝██║ ╚═╝ ██║███████╗
 ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝

 ███╗   ███╗ █████╗ ████████╗ ██████╗██╗  ██╗███████╗██████╗
 ████╗ ████║██╔══██╗╚══██╔══╝██╔════╝██║  ██║██╔════╝██╔══██╗
 ██╔████╔██║███████║   ██║   ██║     ███████║█████╗  ██████╔╝
 ██║╚██╔╝██║██╔══██║   ██║   ██║     ██╔══██║██╔══╝  ██╔══██╗
 ██║ ╚═╝ ██║██║  ██║   ██║   ╚██████╗██║  ██║███████╗██║  ██║
 ╚═╝     ╚═╝╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝

EOF
    echo -e "${NC}"
    echo -e "${BOLD}        Crazy Stuff with Resumes and Cover letters${NC}"
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# Print status message
status() {
    echo -e "${GREEN}[✓]${NC} $1" >&2
}

# Print info message
info() {
    echo -e "${BLUE}[i]${NC} $1" >&2
}

# Print warning message
warn() {
    echo -e "${YELLOW}[!]${NC} $1" >&2
}

# Print error message
error() {
    echo -e "${RED}[✗]${NC} $1" >&2
}

# Docker-style secret loader: supports VAR or VAR_FILE
file_env() {
    local var="$1"
    local def="${2:-}"
    local file_var="${var}_FILE"

    if [ -n "${!var:-}" ] && [ -n "${!file_var:-}" ]; then
        error "Both $var and $file_var are set (but are exclusive)"
        exit 1
    fi

    local val="$def"
    if [ -n "${!var:-}" ]; then
        val="${!var}"
    elif [ -n "${!file_var:-}" ]; then
        if [ ! -r "${!file_var}" ]; then
            error "Cannot read ${!file_var} for $file_var"
            exit 1
        fi
        val="$(< "${!file_var}")"
    fi

    export "$var"="$val"
    unset "$file_var"
}

normalize_log_level() {
    local value="${1^^}"
    local fallback="${2}"
    local name="${3}"

    case "$value" in
        CRITICAL|ERROR|WARNING|INFO|DEBUG)
            echo "$value"
            ;;
        *)
            warn "Invalid ${name}='$1', using ${fallback}"
            echo "$fallback"
            ;;
    esac
}

# Exit code to propagate from failed child processes
EXIT_CODE=0

# Cleanup function for graceful shutdown
cleanup() {
    # Prevent re-entry from signals during cleanup
    trap '' SIGTERM SIGINT SIGQUIT

    echo "" >&2
    info "Shutting down Resume Matcher..."

    # Kill frontend if running
    if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        kill "$FRONTEND_PID" 2>/dev/null || true
        wait "$FRONTEND_PID" 2>/dev/null || true
    fi

    # Kill backend if running
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi

    status "Shutdown complete"
    exit "${EXIT_CODE}"
}

# Initialize PIDs so cleanup doesn't fail on early exit
BACKEND_PID=""
FRONTEND_PID=""

# Set up signal handlers
trap cleanup SIGTERM SIGINT SIGQUIT

# Print banner
print_banner

# Display routing configuration
info "Routing configuration:"
echo -e "  Public port:   ${BOLD}${FRONTEND_PORT}${NC}"
echo -e "  Internal API:  ${BOLD}${BACKEND_PORT}${NC} (proxied at /api)"
echo ""

# Resolve env vars and optional *_FILE secret mounts
info "Loading configuration from environment and *_FILE secrets..."
file_env "LOG_LEVEL" "INFO"
file_env "LOG_LLM" "WARNING"

file_env "LLM_PROVIDER" "openai"

# Only resolve optional LLM_* vars if they (or their *_FILE variants) are provided,
# so we don't override backend defaults with empty strings.
if [ -n "${LLM_MODEL:-}" ] || [ -n "${LLM_MODEL_FILE:-}" ]; then
    file_env "LLM_MODEL"
fi

if [ -n "${LLM_API_KEY:-}" ] || [ -n "${LLM_API_KEY_FILE:-}" ]; then
    file_env "LLM_API_KEY"
fi

if [ -n "${LLM_API_BASE:-}" ] || [ -n "${LLM_API_BASE_FILE:-}" ]; then
    file_env "LLM_API_BASE"
fi

# Optional MongoDB backend (blank/unset = local SQLite). Guarded like the
# LLM_* overrides so empty values don't shadow backend defaults.
if [ -n "${MONGODB_URI:-}" ] || [ -n "${MONGODB_URI_FILE:-}" ]; then
    file_env "MONGODB_URI"
fi

if [ -n "${MONGODB_DATABASE:-}" ] || [ -n "${MONGODB_DATABASE_FILE:-}" ]; then
    file_env "MONGODB_DATABASE"
fi

# On Render, default the public base URL (used for CORS) to the platform URL
# unless FRONTEND_BASE_URL was set explicitly.
if [ -z "${FRONTEND_BASE_URL:-}" ] && [ -n "${RENDER_EXTERNAL_URL:-}" ]; then
    export FRONTEND_BASE_URL="${RENDER_EXTERNAL_URL}"
    info "FRONTEND_BASE_URL not set, using RENDER_EXTERNAL_URL: ${BOLD}${FRONTEND_BASE_URL}${NC}"
fi
APP_LOG_LEVEL="$(normalize_log_level "${LOG_LEVEL}" "INFO" "LOG_LEVEL")"
LLM_LOG_LEVEL="$(normalize_log_level "${LOG_LLM}" "WARNING" "LOG_LLM")"
export LOG_LEVEL="${APP_LOG_LEVEL}"
export LOG_LLM="${LLM_LOG_LEVEL}"
UVICORN_LOG_LEVEL="$(echo "${APP_LOG_LEVEL}" | tr '[:upper:]' '[:lower:]')"
info "Application log level: ${BOLD}${LOG_LEVEL}${NC}"
info "LiteLLM log level:     ${BOLD}${LOG_LLM}${NC}"
if [ "${LOG_LLM}" = "DEBUG" ]; then
    warn "LOG_LLM=DEBUG may log API keys in plaintext. Do not use in production."
fi
status "Configuration loaded"

# Check and create data directory
info "Checking data directory..."
DATA_DIR="/app/backend/data"
if [ ! -d "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR"
    status "Created data directory: $DATA_DIR"
else
    status "Data directory exists: $DATA_DIR"
fi

# Check for Playwright browsers
info "Checking Playwright browsers..."
if [ -d "/root/.cache/ms-playwright" ] || [ -d "/home/appuser/.cache/ms-playwright" ]; then
    status "Playwright browsers found"
else
    warn "Installing Playwright Chromium (this may take a moment)..."
    python -m playwright install chromium || {
        warn "Playwright install failed — PDF export may not work"
    }
    status "Playwright setup complete"
fi

# Start backend
echo ""
info "Starting backend server on internal port ${BACKEND_PORT}..."
cd /app/backend
trap '' SIGTERM SIGINT SIGQUIT
python -m uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT}" --log-level "${UVICORN_LOG_LEVEL}" &
BACKEND_PID=$!
trap cleanup SIGTERM SIGINT SIGQUIT

# Wait for backend to be ready. Cold imports (notably LiteLLM) can take well
# over a minute on throttled free-tier CPUs, so allow several minutes.
# Overridable via BACKEND_STARTUP_TIMEOUT (seconds, minimum 10).
BACKEND_STARTUP_TIMEOUT="${BACKEND_STARTUP_TIMEOUT:-180}"
if ! [[ "${BACKEND_STARTUP_TIMEOUT}" =~ ^[0-9]+$ ]] || [ "${BACKEND_STARTUP_TIMEOUT}" -lt 10 ]; then
    warn "Invalid BACKEND_STARTUP_TIMEOUT='${BACKEND_STARTUP_TIMEOUT}', using 180"
    BACKEND_STARTUP_TIMEOUT="180"
fi
info "Waiting for backend to be ready (timeout: ${BACKEND_STARTUP_TIMEOUT}s)..."
BACKEND_READY=""
for ((i = 1; i <= BACKEND_STARTUP_TIMEOUT; i++)); do
    if curl -s --max-time 2 "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" > /dev/null 2>&1; then
        status "Backend is ready (PID: $BACKEND_PID, took ${i}s)"
        BACKEND_READY="1"
        break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        error "Backend process (PID: $BACKEND_PID) died during startup (check the traceback above)"
        exit 1
    fi
    if [ $((i % 30)) -eq 0 ]; then
        info "Still waiting for backend... (${i}s elapsed)"
    fi
    sleep 1
done
if [ -z "${BACKEND_READY}" ]; then
    error "Backend failed to respond within ${BACKEND_STARTUP_TIMEOUT} seconds"
    if kill -0 "$BACKEND_PID" 2>/dev/null; then
        error "Backend process is still alive (PID: $BACKEND_PID) — likely still importing on a slow CPU."
    else
        error "Backend process is no longer running — check the traceback in the logs above."
    fi
    if (echo > "/dev/tcp/127.0.0.1/${BACKEND_PORT}") 2>/dev/null; then
        info "Port ${BACKEND_PORT} is accepting connections (app may be stuck in startup)."
    else
        warn "Nothing is listening on port ${BACKEND_PORT}."
    fi
    exit 1
fi

# Start frontend
echo ""
info "Starting frontend server on port ${FRONTEND_PORT}..."
cd /app/frontend

# Next.js uses PORT environment variable
export HOSTNAME="0.0.0.0"
export PORT="${FRONTEND_PORT}"
if [ ! -f "server.js" ]; then
    error "Missing frontend standalone server.js. Rebuild the Docker image."
    exit 1
fi

trap '' SIGTERM SIGINT SIGQUIT
node server.js "$@" &
FRONTEND_PID=$!
trap cleanup SIGTERM SIGINT SIGQUIT
status "Frontend is running (PID: $FRONTEND_PID)"

# Wait for either process to exit, but ignore errexit for this wait
set +e
wait -n "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
EXIT_CODE=$?
set -e
warn "A process exited unexpectedly (exit code: ${EXIT_CODE}), shutting down..."
cleanup
