# Dockerfile - MT5 dual-terminal algo trading bridge, containerized.
#
# Build stage compiles the SpotDump EA with the MetaEditor that ships inside
# the official MetaTrader 5 installer and bakes a wineprefix + MT5 binaries
# into the image.  Every wine invocation is wrapped in `timeout` - a hung
# wine step must NEVER stall a build or a boot.  If the installer cannot run
# at build time (stub download, headless quirk), the build still succeeds and
# the entrypoint retries at runtime or the user mounts an existing prefix.
#
# Runtime stage = Debian + wine + Xvfb + python.  Works under docker AND
# rootless podman (podman roots without user namespaces skip wineboot and
# reuse the prefix baked below).

FROM debian:bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wine wine64 xvfb p7zip-full \
 && rm -rf /var/lib/apt/lists/*
ENV WINEDEBUG=-all WINEPREFIX=/prefix WINEARCH=win64 HOME=/root \
    MT5DIR="/prefix/drive_c/Program Files/MetaTrader 5"

# Official installer (may be the full payload or a small web-stub)
RUN curl -fL --retry 5 --retry-all-errors -C - \
      -o /tmp/mt5setup.exe \
      https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe

# Prefix init - hard timeout, non-fatal on hang (runtime retries); xvfb-run
# gives wine a display so nothing blocks on X
RUN timeout 200 xvfb-run -a wineboot -i >/dev/null 2>&1 \
    || timeout 60 xvfb-run -a wineboot >/dev/null 2>&1 || true
RUN wineserver -w 2>/dev/null || true

# MT5 files: plain 7z extraction first; silent wine install as fallback
RUN mkdir -p "$MT5DIR" \
 && (7z x -y -o"$MT5DIR" /tmp/mt5setup.exe >/dev/null 2>&1 || true) \
 && if [ ! -f "$MT5DIR/terminal64.exe" ]; then \
      echo "no terminal64.exe after extract - trying silent install (5 min cap)"; \
      timeout 300 xvfb-run -a wine64 /tmp/mt5setup.exe /auto >/dev/null 2>&1 || true; \
      wineserver -w 2>/dev/null || true; \
    fi; \
    if [ -f "$MT5DIR/terminal64.exe" ]; then echo "MT5 binaries present"; \
    else echo "WARNING: MT5 not extracted at build time - runtime will install it"; fi

# Stage the MT5 tree at a SPACE-FREE path: buildah's COPY splits the
# --from source on spaces even when backslash-escaped, so "/prefix/drive_c/
# Program Files/..." can never be copied directly.
RUN test -d "$MT5DIR" && cp -a "$MT5DIR" /mt5-master || mkdir -p /mt5-master

# EA compile - best effort; the runtime compiles on first boot if skipped
COPY SpotDump.mq5 /src/SpotDump.mq5
RUN if [ -f "$MT5DIR/metaeditor64.exe" ]; then \
      mkdir -p "$MT5DIR/MQL5/Experts" \
      && cp /src/SpotDump.mq5 "$MT5DIR/MQL5/Experts/" \
      && cd "$MT5DIR" \
      && timeout 120 wine64 metaeditor64.exe \
           /compile:"C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\SpotDump.mq5" \
           /log >/dev/null 2>&1 || true; \
      if [ -f MQL5/Experts/SpotDump.ex5 ]; then echo "EA compiled"; \
      else echo "EA compile skipped (runtime will retry)"; fi; \
    else echo "no metaeditor at build time - EA compiles at runtime"; fi

# Runtime: trixie for wine 10.x - its new WoW64 runs 64-bit terminal64.exe
# WITHOUT 32-bit libs (wine32:i386 is unbuildable/unavailable on modern
# Debian multiarch; wow64-only mirrors the Fedora wine-wow64 this stack was
# tuned on).  No build-essential: requirements install from binary wheels.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv \
      wine64 \
      xvfb x11-utils p7zip-full \
      curl sqlite3 ca-certificates procps psmisc iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -ms /bin/bash mt5
# trixie's wine64 has NO /usr/bin/wine* (they come from the ia32-dependent
# `wine` metapackage) - bridge the loader/wineserver to the expected paths
RUN ln -sf /usr/lib/wine/wine64 /usr/bin/wine64 \
 && ln -sf /usr/lib/wine/wine64 /usr/bin/wine \
 && ln -sf /usr/lib/wine/wineserver /usr/bin/wineserver

# Python deps into a venv at the same path the native install uses
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --prefer-binary \
      --upgrade pip wheel setuptools \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --prefer-binary \
      -r /app/requirements.txt

# Baked prefix + whatever MT5 files the build stage produced (may be partial)
COPY --from=build /prefix/ /mt5/prefix/
COPY --from=build /mt5-master/ /mt5/master/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY . /app/
# /data is pre-chowned so a named volume inherits mt5 ownership (podman
# honours image dir ownership for named volumes; docker chowns to root but
# the entrypoint fixes it when it lands as root).
RUN chmod +x /app/*.py /app/run.sh /usr/local/bin/docker-entrypoint.sh 2>/dev/null; \
    mkdir -p /data && chown mt5:mt5 /data /app

ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH \
    WINEPREFIX=/data/prefix WINEARCH=win64 WINEDEBUG=-all \
    DISPLAY=:99 MT5_WINEPREFIX=/data/prefix \
    MT5_WEB_HOST=0.0.0.0
VOLUME ["/data"]
EXPOSE 8000
WORKDIR /app
USER mt5

HEALTHCHECK --interval=30s --timeout=5s \
  CMD curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["all"]
