FROM python:3.12-slim

WORKDIR /app

# gcc + libc6-dev + libffi-dev: cryptography (pulled in by pyjwt[crypto],
# for the OAuth token machinery) ships prebuilt wheels for amd64/arm64,
# but not for 32-bit ARM (armv7 - e.g. a Raspberry Pi 2, or a Pi 3
# running 32-bit Raspberry Pi OS) - there it falls back to building cffi
# from source, which needs a C compiler and libc's headers. Confirmed
# live: --no-install-recommends drops libc6-dev too (it's only a
# "Recommends" of gcc on Debian, not a hard dependency) and the build
# fails with "fatal error: stdlib.h: No such file or directory" without
# it - so it's listed explicitly here rather than relying on gcc to pull
# it in. Removed again after pip install to keep the image lean - only
# needed to build the wheel, not to run it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev libffi-dev

COPY halaxy_mcp.py .

RUN useradd --create-home --uid 1000 halaxy \
    && chown -R halaxy:halaxy /app \
    && mkdir -p /data \
    && chown halaxy:halaxy /data
USER halaxy

# MCP_HOST must be 0.0.0.0 in a container - the script's own default
# (127.0.0.1) is deliberately safe for direct/non-Docker use, but that
# would make the server unreachable from outside the container's network
# namespace. Override the transport, not the secrets (HALAXY_CLIENT_ID/
# SECRET, MCP_LOGIN_USERNAME/PASSWORD) - those are supplied at
# `docker run`/compose time, never baked into the image.
#
# MCP_OAUTH_STATE_FILE points at /data rather than /app - /app gets
# overwritten by COPY on every image rebuild, but /data is where the
# compose files mount a persistent volume, so registered OAuth clients
# and access tokens survive a `docker compose up --build` instead of
# forcing every connected client to reconnect on every code update.
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_OAUTH_STATE_FILE=/data/oauth_state.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["python3", "halaxy_mcp.py"]
