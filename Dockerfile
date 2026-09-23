# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8080

# ca-certificates: base trust store. MAX additionally serves a chain rooted in the Russian
# Ministry of Digital Development CA; that root ships in certs/ and is loaded on top of it,
# so TLS verification never has to be relaxed (override the path with SSL_CA_BUNDLE).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --home-dir /app --shell /usr/sbin/nologin bot

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=bot:bot . .
RUN mkdir -p data certs && chown -R bot:bot /app

USER bot
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1

CMD ["python", "main.py", "--mode=webhook"]
