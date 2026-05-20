import asyncio
import base64
import concurrent.futures
import email
import email.parser
import email.policy
import logging
import os
import pathlib
import secrets
import ssl
import sys

import httpx
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: required environment variable {name!r} is not set", file=sys.stderr)
        sys.exit(1)
    return val

RELAY_USER   = _require("RELAY_USER")
RELAY_PASS   = _require("RELAY_PASS")
CF_API_TOKEN = _require("CF_API_TOKEN")
CF_ACCOUNT_ID = _require("CF_ACCOUNT_ID")
CERT_DOMAIN  = _require("CERT_DOMAIN")

LISTEN_HOST  = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT  = int(os.environ.get("LISTEN_PORT", "2525"))
LOG_LEVEL    = os.environ.get("LOG_LEVEL", "INFO").upper()

CF_SEND_URL  = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/email/sending/send"
)

FLAG_PATH    = pathlib.Path("/certs/renewed")
CERT_BASE    = pathlib.Path("/certs/live")

PASSTHROUGH_HEADERS = {
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "References",
    "In-Reply-To",
}

_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=10)

# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def build_ssl_context(domain: str) -> ssl.SSLContext:
    cert_dir = CERT_BASE / domain
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(
        certfile=cert_dir / "fullchain.pem",
        keyfile=cert_dir / "privkey.pem",
    )
    return ctx

# ---------------------------------------------------------------------------
# SASL authenticator
# ---------------------------------------------------------------------------

def authenticator(server, session, envelope, mechanism, auth_data):
    if mechanism not in ("PLAIN", "LOGIN"):
        return AuthResult(success=False, handled=True)
    if not isinstance(auth_data, LoginPassword):
        return AuthResult(success=False, handled=True)
    user_ok = secrets.compare_digest(auth_data.login, RELAY_USER.encode())
    pass_ok = secrets.compare_digest(auth_data.password, RELAY_PASS.encode())
    return AuthResult(success=user_ok and pass_ok, handled=True)

# ---------------------------------------------------------------------------
# Cloudflare client (synchronous — used via run_in_executor)
# ---------------------------------------------------------------------------

class CloudflareClient:
    def __init__(self):
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            timeout=30.0,
        )

    def send(self, payload: dict) -> tuple[int, bool]:
        try:
            resp = self._client.post(CF_SEND_URL, json=payload)
            try:
                data = resp.json()
                success = bool(data.get("success", False))
            except Exception:
                success = resp.status_code == 200
            return resp.status_code, success
        except httpx.TimeoutException:
            logging.warning("Cloudflare API request timed out")
            return 451, False
        except httpx.RequestError as exc:
            logging.warning("Cloudflare API request error: %s", exc)
            return 451, False

    def close(self):
        self._client.close()


_cf = CloudflareClient()

# ---------------------------------------------------------------------------
# MIME → Cloudflare JSON payload
# ---------------------------------------------------------------------------

def build_cf_payload(envelope, recipient: str) -> dict:
    raw: bytes = envelope.original_content
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)

    payload: dict = {
        "from": envelope.mail_from,
        "to": recipient,
        "subject": str(msg.get("Subject", "")),
    }

    if msg.is_multipart():
        attachments = []
        for part in msg.walk():
            ct = part.get_content_type()
            if part.get_content_maintype() == "multipart":
                continue
            if ct == "text/plain" and "text" not in payload:
                payload["text"] = part.get_content()
            elif ct == "text/html" and "html" not in payload:
                payload["html"] = part.get_content()
            elif part.get_content_maintype() not in ("text",):
                raw_bytes = part.get_payload(decode=True)
                if raw_bytes:
                    attachments.append({
                        "filename": part.get_filename() or "attachment",
                        "type": ct,
                        "content": base64.b64encode(raw_bytes).decode(),
                    })
        if attachments:
            payload["attachments"] = attachments
    else:
        ct = msg.get_content_type()
        if ct == "text/html":
            payload["html"] = msg.get_content()
        else:
            payload["text"] = msg.get_content()

    # Passthrough headers — Message-ID is intentionally excluded
    custom_headers = {}
    for key in PASSTHROUGH_HEADERS:
        val = msg.get(key)
        if val:
            custom_headers[key] = str(val)
    if custom_headers:
        payload["headers"] = custom_headers

    return payload


def smtp_response(http_status: int, success: bool) -> str:
    if http_status == 200 and success:
        return "250 OK"
    if http_status == 429:
        return "451 4.4.5 Rate limited, try again later"
    if http_status in (400, 403):
        return "550 5.7.1 Message rejected by upstream"
    return "451 4.4.0 Upstream error, try again later"

# ---------------------------------------------------------------------------
# SMTP handler
# ---------------------------------------------------------------------------

class RelayHandler:
    async def handle_DATA(self, server, session, envelope):
        loop = asyncio.get_running_loop()
        last_response = "250 OK"
        for recipient in envelope.rcpt_tos:
            try:
                payload = build_cf_payload(envelope, recipient)
            except Exception:
                logging.exception("Failed to build CF payload for %s", recipient)
                return "451 4.0.0 Internal error during message parsing"

            try:
                status, ok = await loop.run_in_executor(
                    _thread_pool, _cf.send, payload
                )
            except Exception:
                logging.exception("Unexpected error calling Cloudflare API")
                return "451 4.0.0 Internal relay error"

            response = smtp_response(status, ok)
            logging.info(
                "CF API %s success=%s → %s (to=%s from=%s)",
                status, ok, response, recipient, envelope.mail_from,
            )
            if not (status == 200 and ok):
                return response
            last_response = response

        return last_response

# ---------------------------------------------------------------------------
# Cert watcher
# ---------------------------------------------------------------------------

async def cert_watcher():
    while True:
        await asyncio.sleep(60)
        if FLAG_PATH.exists():
            logging.info("Cert renewal flag detected — exiting for restart")
            FLAG_PATH.unlink(missing_ok=True)
            sys.exit(0)  # Docker restart policy reloads the process with fresh certs

# ---------------------------------------------------------------------------
# Startup cert wait
# ---------------------------------------------------------------------------

async def wait_for_cert(domain: str, timeout: int = 120, interval: int = 5):
    cert_file = CERT_BASE / domain / "fullchain.pem"
    elapsed = 0
    while not cert_file.exists():
        if elapsed >= timeout:
            logging.error("Cert not found after %ds: %s — exiting", timeout, cert_file)
            sys.exit(1)
        logging.warning("Waiting for cert (%ds elapsed): %s", elapsed, cert_file)
        await asyncio.sleep(interval)
        elapsed += interval
    logging.info("Cert found: %s", cert_file)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    await wait_for_cert(CERT_DOMAIN)

    ssl_ctx = build_ssl_context(CERT_DOMAIN)
    handler = RelayHandler()

    controller = Controller(
        handler,
        hostname=LISTEN_HOST,
        port=LISTEN_PORT,
        tls_context=ssl_ctx,
        auth_required=True,
        auth_require_tls=True,
        authenticator=authenticator,
    )
    controller.start()
    logging.info("SMTP relay listening on %s:%d", LISTEN_HOST, LISTEN_PORT)

    watcher = asyncio.create_task(cert_watcher())

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        watcher.cancel()
        controller.stop()
        _cf.close()
        logging.info("Relay shut down")


if __name__ == "__main__":
    asyncio.run(main())
