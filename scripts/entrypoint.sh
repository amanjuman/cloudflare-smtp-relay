#!/bin/sh
set -e

CERT_FILE="/certs/live/${CERT_DOMAIN}/fullchain.pem"
MAX_WAIT=120
INTERVAL=5
elapsed=0

echo "Waiting for cert at ${CERT_FILE}..."

while [ ! -f "${CERT_FILE}" ]; do
    if [ "${elapsed}" -ge "${MAX_WAIT}" ]; then
        echo "ERROR: Cert not found after ${MAX_WAIT}s — exiting." >&2
        exit 1
    fi
    echo "  Still waiting... (${elapsed}s elapsed)"
    sleep "${INTERVAL}"
    elapsed=$((elapsed + INTERVAL))
done

echo "Cert found. Starting relay..."
exec python -u /app/relay.py
