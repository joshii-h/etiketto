# Tavletta print server: REST API and MCP, no window (docs/server.md).
#
#   docker build -f packaging/docker/Dockerfile -t tavletta-server .
#   docker run -e TAVLETTA_TOKEN=... -v ./labels:/labels -p 127.0.0.1:8790:8790 tavletta-server
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
COPY pyproject.toml setup.py MANIFEST.in README.md LICENSE THIRD_PARTY_NOTICES.md CREDITS.md ./
COPY tavletta ./tavletta
COPY data ./data
COPY po ./po
# setup.py compiles the translations (msgfmt); git fetches the VC-500W protocol library
RUN python3 -m venv --system-site-packages /opt/tavletta \
    && /opt/tavletta/bin/pip install --no-cache-dir . \
    && /opt/tavletta/bin/tavletta --version

FROM debian:${DEBIAN}
# Security fixes newer than the base image (the release workflow builds
# without cache, so they are current). Poppler draws the PDF pages of shipping
# labels (tavletta serve --allow-pdf-pages): about 35 MB with its libraries.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gir1.2-poppler-0.18 \
    && rm -rf /var/lib/apt/lists/*
# Fonts: a label drawn in a font the server lacks is drawn in another one
# (and the preview and the job warn: font_missing). These cover common
# office and label fonts in about 15 MB: DejaVu, Source Sans 3, Liberation
# (metrics of Arial, Times New Roman, Courier New) and Carlito (Calibri).
# More Debian packages: --build-arg FONTS="... fonts-noto-core" (about
# 40 MB more); fonts of your own: mount a folder at /usr/local/share/fonts.
ARG FONTS="fonts-dejavu-core fonts-adobe-sourcesans3 fonts-liberation fonts-crosextra-carlito"
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-gi python3-gi-cairo gir1.2-pango-1.0 fontconfig ${FONTS} \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /data --create-home tavletta \
    && install -d -o tavletta -g tavletta -m 0755 /labels /config /usr/local/share/fonts
COPY --from=build /opt/tavletta /opt/tavletta
# the licence, the notices of what Tavletta bundles and the credits
COPY --from=build /src/LICENSE /src/THIRD_PARTY_NOTICES.md /src/CREDITS.md /usr/share/doc/tavletta/
# Mount the labels at /labels and printers.toml, tokens.toml (and, if
# wanted, tavletta/settings.json of the app) at /config. Both are plain
# folders, no volumes: Compose (or docker run -v) says what goes there, and
# any user id can read them (compose.yaml runs as the owner of the files).
# MALLOC_ARENA_MAX: threads do not each keep memory a large label used.
# Fonts mounted at /usr/local/share/fonts are found when the server starts
# (fontconfig reads that folder; its cache goes to /tmp, as / is read-only).
ENV PATH=/opt/tavletta/bin:$PATH \
    XDG_CONFIG_HOME=/config \
    XDG_CACHE_HOME=/tmp \
    MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1
USER tavletta
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/api/v1/health', timeout=4)"]
# all interfaces of the container: refused without TAVLETTA_TOKEN (or --token-file)
ENTRYPOINT ["tavletta", "serve", "--host", "0.0.0.0", "--port", "8790", "--labels-dir", "/labels"]
CMD ["--mcp"]
