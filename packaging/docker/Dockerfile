# Etiketto print server: REST API and MCP, no window (docs/server.md).
#
#   docker build -f packaging/docker/Dockerfile -t etiketto-server .
#   docker run -e ETIKETTO_TOKEN=... -v ./labels:/labels -p 127.0.0.1:8790:8790 etiketto-server
#
# Rendering needs Pango and Cairo through PyGObject, not GTK: the Debian
# packages of the same Python are used (python3-gi is built for it), the
# Python dependencies go into a venv that sees them.

ARG DEBIAN=trixie-slim

FROM debian:${DEBIAN} AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-gi python3-gi-cairo gir1.2-pango-1.0 gettext git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml setup.py MANIFEST.in README.md LICENSE ./
COPY etiketto ./etiketto
COPY data ./data
COPY po ./po
# setup.py compiles the translations (msgfmt); git fetches the VC-500W protocol library
RUN python3 -m venv --system-site-packages /opt/etiketto \
    && /opt/etiketto/bin/pip install --no-cache-dir . \
    && /opt/etiketto/bin/etiketto --version

FROM debian:${DEBIAN}
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-gi python3-gi-cairo gir1.2-pango-1.0 fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /data --create-home etiketto \
    && install -d -o etiketto -g etiketto -m 0700 /labels /data/config
COPY --from=build /opt/etiketto /opt/etiketto
# settings.json (printer addresses, safety margin) in /data/config/etiketto
ENV PATH=/opt/etiketto/bin:$PATH \
    XDG_CONFIG_HOME=/data/config \
    PYTHONUNBUFFERED=1
USER etiketto
VOLUME ["/labels", "/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/api/v1/health', timeout=4)"]
# all interfaces of the container: refused without ETIKETTO_TOKEN (or --token-file)
ENTRYPOINT ["etiketto", "serve", "--host", "0.0.0.0", "--port", "8790", "--labels-dir", "/labels"]
CMD ["--mcp"]
