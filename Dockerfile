FROM python:3.12-slim

RUN pip install --no-cache-dir aiosmtpd==1.4.6 httpx==0.27.2

RUN useradd --system --no-create-home --shell /usr/sbin/nologin relay

WORKDIR /app

COPY relay.py .
COPY scripts/ ./scripts/

RUN chmod +x scripts/entrypoint.sh

USER relay

EXPOSE 2525

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import socket,os; s=socket.socket(); s.connect(('127.0.0.1',int(os.environ.get('LISTEN_PORT','2525')))); s.close()" || exit 1

ENTRYPOINT ["scripts/entrypoint.sh"]
