# cf-smtp-relay

A self-hosted, Dockerised SMTP relay that authenticates senders and forwards outbound mail to **Cloudflare Email Sending** via REST API. TLS certificates are provisioned automatically from **Let's Encrypt** using a Cloudflare DNS-01 challenge and renewed every 12 hours.

---

## How It Works

```
Client app
   │  SMTP :2525 + STARTTLS + SASL auth
   ▼
[relay container]  relay.py (aiosmtpd + httpx)
   │  Shared /certs volume
[certbot container]  initial cert + 12 h renewal loop
   │  HTTPS POST
   ▼
Cloudflare Email Sending REST API
```

- The **relay** container listens on port 2525, requires STARTTLS before accepting credentials, and forwards each message to the Cloudflare Email Sending API.
- The **certbot** container provisions a Let's Encrypt certificate on first boot using the Cloudflare DNS-01 challenge, then loops every 12 hours to renew it.
- Both containers share a named Docker volume (`certs`) for the certificate files.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose v2 | `docker compose version` to verify |
| A domain managed in Cloudflare | DNS must be on Cloudflare for the DNS-01 challenge |
| A subdomain pointed at this server | e.g. `smtp.yourdomain.com` → server public IP |
| Cloudflare Email Sending enabled | Dashboard → Account → Email Sending |
| Two Cloudflare API tokens | See below |

### Cloudflare API Tokens

Create two tokens at **Cloudflare Dashboard → My Profile → API Tokens → Create Token**:

**Token 1 — Email Sending** (`CF_API_TOKEN`)

| Permission | |
|---|---|
| Account → Email Sending → Edit | |

**Token 2 — DNS (for certbot)** (`CF_DNS_API_TOKEN`)

| Permission | Resource |
|---|---|
| Zone → Zone → Read | All zones |
| Zone → DNS → Edit | All zones |

---

## Deployment

### 1. Clone the repository

```bash
git clone https://github.com/your-org/cf-smtp-relay.git
cd cf-smtp-relay
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in all values:

```env
# Cloudflare Email Sending
CF_ACCOUNT_ID=your-cloudflare-account-id
CF_API_TOKEN=your-email-sending-api-token

# Cloudflare DNS (for Let's Encrypt DNS-01 challenge)
CF_DNS_API_TOKEN=your-dns-api-token

# SMTP auth credentials — clients use these to authenticate
RELAY_USER=relay
RELAY_PASS=your-strong-password-here

# TLS / Let's Encrypt
CERT_DOMAIN=smtp.yourdomain.com   # must resolve to this server's public IP
ACME_EMAIL=admin@yourdomain.com   # used for Let's Encrypt account registration

# Optional
LISTEN_HOST=0.0.0.0
LISTEN_PORT=2525
LOG_LEVEL=INFO
```

> **Security:** keep `.env` out of version control — it is listed in `.gitignore`.

### 3. Open the firewall

Ensure TCP port **2525** is reachable from the clients that will send mail through the relay.

### 4. Build and start

```bash
docker compose build
docker compose up -d
```

On first boot the certbot container provisions the TLS certificate (allow 30–60 seconds for DNS propagation). The relay container waits for the certificate before starting.

### 5. Watch the logs

```bash
docker compose logs -f
```

You should see:

```
cf-certbot   | Provisioning new certificate for smtp.yourdomain.com...
cf-certbot   | Successfully received certificate.
cf-smtp-relay | Cert found. Starting relay...
cf-smtp-relay | SMTP relay listening on 0.0.0.0:2525
```

---

## Testing

### Quick STARTTLS check

```bash
openssl s_client -connect smtp.yourdomain.com:2525 -starttls smtp
```

Look for `CN=smtp.yourdomain.com` and `verify return:1` in the output.

### Send a test email

Using `swaks`:

```bash
swaks \
  --to recipient@example.com \
  --from sender@yourdomain.com \
  --server smtp.yourdomain.com \
  --port 2525 \
  --tls \
  --auth-user relay \
  --auth-password YOUR_RELAY_PASS \
  --tls-verify
```

Expected response: `250 OK`.

Verify delivery in **Cloudflare Dashboard → Email Sending → Logs**.

---

## SMTP Client Configuration

| Setting | Value |
|---|---|
| SMTP Host | `smtp.yourdomain.com` |
| Port | `2525` |
| Security | STARTTLS |
| Authentication | PLAIN or LOGIN |
| Username | value of `RELAY_USER` |
| Password | value of `RELAY_PASS` |

---

## Certificate Renewal

Certificates are renewed automatically. The certbot container runs a renewal check every 12 hours. When a renewal succeeds, it signals the relay by writing `/certs/renewed`. The relay detects this flag within 60 seconds, removes it, and restarts cleanly — Docker's `restart: unless-stopped` policy brings it back up with the fresh certificate.

No manual intervention is required.

---

## Stopping and Updating

```bash
# Stop
docker compose down

# Pull latest code and rebuild
git pull
docker compose build
docker compose up -d
```

Certificate data is stored in the `certs` Docker named volume and persists across container restarts and rebuilds.

---

## File Structure

```
cf-smtp-relay/
├── relay.py               # SMTP relay — aiosmtpd + httpx
├── Dockerfile             # relay container image
├── docker-compose.yml     # relay + certbot services
├── scripts/
│   ├── entrypoint.sh      # waits for cert, then starts relay.py
│   └── certbot-run.sh     # provisions cert and runs renewal loop
└── .env.example           # environment variable template
```

---

## Troubleshooting

**Relay keeps waiting for cert**

- Check certbot logs: `docker logs cf-certbot`
- Ensure `CERT_DOMAIN` resolves to this server's public IP
- Verify `CF_DNS_API_TOKEN` has `Zone:Zone:Read` + `Zone:DNS:Edit` permissions

**Authentication rejected**

- Confirm the client is negotiating STARTTLS before sending `AUTH`
- Auth over plain (non-TLS) connections is intentionally rejected

**Cloudflare API errors**

- `550` — message rejected; check `CF_API_TOKEN` has Email Sending Edit permission and the sender domain is onboarded in Cloudflare Email Sending
- `451` — temporary failure; the relay will return a retryable error and the sending MTA will retry

**Certificate permission errors**

- If the relay cannot read cert files, run:
  ```bash
  docker run --rm -v cf-smtp-relay_certs:/certs alpine sh -c "
    chmod 755 /certs/live /certs/live/<CERT_DOMAIN> /certs/archive /certs/archive/<CERT_DOMAIN>
    chmod 644 /certs/archive/<CERT_DOMAIN>/*.pem
  "
  ```
  Then restart: `docker compose restart relay`
