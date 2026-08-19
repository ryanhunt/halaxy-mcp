FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY halaxy_mcp.py .

RUN useradd --create-home --uid 1000 halaxy && chown -R halaxy:halaxy /app
USER halaxy

# MCP_HOST must be 0.0.0.0 in a container - the script's own default
# (127.0.0.1) is deliberately safe for direct/non-Docker use, but that
# would make the server unreachable from outside the container's network
# namespace. Override the transport, not the secrets (HALAXY_CLIENT_ID/
# SECRET, MCP_SERVER_TOKEN) - those are supplied at `docker run`/compose
# time, never baked into the image.
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["python3", "halaxy_mcp.py"]
