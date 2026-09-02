#!/usr/bin/env bash
# smoke_container.sh — Tier-2 container smoke test for the Pharos MCP Server image.
#
# PURPOSE: Validate the real container image end-to-end (build → run → probe).
#   This is NOT wired into pytest.  Run it manually or in a CI job that has
#   podman/docker and a writable OCI socket.
#
# USAGE:
#   bash scripts/smoke_container.sh [IMAGE_TAG]
#   IMAGE_TAG defaults to "pharos:smoke"
#
# EXIT: 0 on success; non-zero on any failure (set -euo pipefail).
#
# WHAT IT CHECKS:
#   1. Build the image.
#   2. Run with -e LUMINO_HTTP_TOKEN=smoke-token -p 8000:8000:
#        a. GET /health          → HTTP 200
#        b. POST /mcp (no token) → HTTP 401
#        c. POST /mcp (with token) → HTTP != 401
#   3. Run WITHOUT LUMINO_HTTP_TOKEN (image's 0.0.0.0 default) → container
#      exits nonzero (fail-closed guard in main.py; M2-pinned).

set -euo pipefail

IMAGE_TAG="${1:-pharos:smoke}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTHED_CONTAINER="pharos-smoke-authed-$$"
NOTOKEN_CONTAINER="pharos-smoke-notoken-$$"
HOST_PORT=18000  # use a non-standard port to avoid conflicts with running instances

# ---------------------------------------------------------------------------
# Detect container runtime (prefer podman, fall back to docker)
# ---------------------------------------------------------------------------
if command -v podman &>/dev/null; then
    CTR=podman
elif command -v docker &>/dev/null; then
    CTR=docker
else
    echo "ERROR: neither podman nor docker found in PATH" >&2
    exit 1
fi
echo "Using container runtime: ${CTR}"

# ---------------------------------------------------------------------------
# Cleanup trap — runs on exit (success or failure)
# ---------------------------------------------------------------------------
cleanup() {
    local ec=$?
    echo "--- cleanup ---"
    ${CTR} rm -f "${AUTHED_CONTAINER}" 2>/dev/null || true
    ${CTR} rm -f "${NOTOKEN_CONTAINER}" 2>/dev/null || true
    # Image is deliberately RETAINED to speed up re-runs; remove manually with:
    #   ${CTR} rmi "${IMAGE_TAG}"
    exit "${ec}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1: Build
# ---------------------------------------------------------------------------
echo "=== Step 1: build ${IMAGE_TAG} ==="
${CTR} build -t "${IMAGE_TAG}" "${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Step 2: Run with LUMINO_HTTP_TOKEN and probe
# ---------------------------------------------------------------------------
echo "=== Step 2: run with token, probe /health + /mcp auth ==="
${CTR} run -d \
    --name "${AUTHED_CONTAINER}" \
    -e LUMINO_HTTP_TOKEN=smoke-token \
    -p "${HOST_PORT}:8000" \
    "${IMAGE_TAG}"

# Wait for the server to start (up to 60 s)
BASE_URL="http://127.0.0.1:${HOST_PORT}"
MAX_WAIT=60
WAITED=0
echo -n "Waiting for server on ${BASE_URL}/health ."
until curl -fsS "${BASE_URL}/health" -o /dev/null 2>/dev/null; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [[ ${WAITED} -ge ${MAX_WAIT} ]]; then
        echo ""
        echo "ERROR: server did not start within ${MAX_WAIT}s" >&2
        ${CTR} logs "${AUTHED_CONTAINER}" >&2 || true
        exit 1
    fi
    echo -n "."
done
echo " ready"

# (a) GET /health → 200
echo "--- (a) GET /health ---"
STATUS=$(curl -fsS -o /dev/null -w "%{http_code}" "${BASE_URL}/health")
if [[ "${STATUS}" != "200" ]]; then
    echo "ERROR: /health returned ${STATUS}, expected 200" >&2
    exit 1
fi
echo "PASS: /health → ${STATUS}"

# (b) POST /mcp with no token → 401
echo "--- (b) POST /mcp no token → 401 ---"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${BASE_URL}/mcp" \
    -H "Content-Type: application/json" \
    -d '{}')
if [[ "${STATUS}" != "401" ]]; then
    echo "ERROR: /mcp (no token) returned ${STATUS}, expected 401" >&2
    exit 1
fi
echo "PASS: /mcp no-token → ${STATUS}"

# (c) POST /mcp with correct Bearer token → not 401
echo "--- (c) POST /mcp with token → not-401 ---"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${BASE_URL}/mcp" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer smoke-token" \
    -d '{}')
if [[ "${STATUS}" == "401" ]]; then
    echo "ERROR: /mcp with correct token still returned 401" >&2
    exit 1
fi
echo "PASS: /mcp with-token → ${STATUS} (not 401)"

# Stop the authed container before reusing the port
${CTR} stop "${AUTHED_CONTAINER}"
${CTR} rm -f "${AUTHED_CONTAINER}"

# ---------------------------------------------------------------------------
# Step 3: Run WITHOUT token → container must exit nonzero (fail-closed, M2)
# ---------------------------------------------------------------------------
echo "=== Step 3: run WITHOUT token (0.0.0.0 default) → must exit nonzero ==="
set +e  # disable errexit for this check
${CTR} run \
    --name "${NOTOKEN_CONTAINER}" \
    "${IMAGE_TAG}"
NOTOKEN_EXIT=$?
set -e

if [[ "${NOTOKEN_EXIT}" -eq 0 ]]; then
    echo "ERROR: container exited 0 without a token — fail-closed guard broken!" >&2
    exit 1
fi
echo "PASS: container exited ${NOTOKEN_EXIT} (non-zero) — fail-closed guard holds"

# ---------------------------------------------------------------------------
# All checks passed
# ---------------------------------------------------------------------------
echo ""
echo "=== Smoke test PASSED ==="
