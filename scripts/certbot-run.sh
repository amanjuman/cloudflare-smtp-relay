#!/bin/sh
set -e

CREDENTIALS="/tmp/cloudflare.ini"
CERT_DIR="/certs"
CERT_PATH="${CERT_DIR}/live/${CERT_DOMAIN}/fullchain.pem"

# Write Cloudflare DNS credentials (chmod 600 required by certbot)
cat > "${CREDENTIALS}" <<EOF
dns_cloudflare_api_token = ${CF_DNS_API_TOKEN}
EOF
chmod 600 "${CREDENTIALS}"

# Initial cert provisioning (skip if cert already exists)
if [ ! -f "${CERT_PATH}" ]; then
    echo "Provisioning new certificate for ${CERT_DOMAIN}..."
    certbot certonly \
        --non-interactive \
        --agree-tos \
        --email "${ACME_EMAIL}" \
        --dns-cloudflare \
        --dns-cloudflare-credentials "${CREDENTIALS}" \
        --dns-cloudflare-propagation-seconds 30 \
        --config-dir "${CERT_DIR}" \
        --work-dir /tmp/certbot-work \
        --logs-dir /tmp/certbot-logs \
        -d "${CERT_DOMAIN}"
    echo "Certificate provisioned."
else
    echo "Certificate already exists at ${CERT_PATH}, skipping initial provisioning."
fi

# Make certs readable by non-root relay user
chmod 755 "${CERT_DIR}/live" "${CERT_DIR}/live/${CERT_DOMAIN}" \
          "${CERT_DIR}/archive" "${CERT_DIR}/archive/${CERT_DOMAIN}"
chmod 644 "${CERT_DIR}/archive/${CERT_DOMAIN}"/*.pem

# Signal relay that cert is ready
touch "${CERT_DIR}/renewed"

# Renewal loop (every 12 hours)
while true; do
    echo "Sleeping 12h before next renewal check..."
    sleep 43200

    echo "Running certificate renewal..."
    if certbot renew \
        --config-dir "${CERT_DIR}" \
        --work-dir /tmp/certbot-work \
        --logs-dir /tmp/certbot-logs \
        --dns-cloudflare-credentials "${CREDENTIALS}" \
        --quiet; then
        chmod 755 "${CERT_DIR}/live" "${CERT_DIR}/live/${CERT_DOMAIN}" \
                  "${CERT_DIR}/archive" "${CERT_DIR}/archive/${CERT_DOMAIN}"
        chmod 644 "${CERT_DIR}/archive/${CERT_DOMAIN}"/*.pem
        touch "${CERT_DIR}/renewed"
        echo "Renewal cycle complete, relay notified."
    else
        echo "certbot renew exited non-zero — check logs."
    fi
done
