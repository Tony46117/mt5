# Dockerfile - MT5 dual-terminal algo trading bridge, containerized.
#
# Build stage compiles the SpotDump EA with the MetaEditor that ships inside
# the official MetaTrader 5 installer, so no compiler setup is needed at
# runtime.  Runtime stage = Debian + wine + Xvfb + python; MT5 itself is
# installed on first boot into the mounted volume.
#
# Multi-arch friendly: TARGETPLATFORM + dpkg --print-foreign-architectures
# add i386 only on amd64 (wine is x86-only; arm64 builds still run the web
# panel + scheduler but cannot run the terminals).

FROM debian:bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl wine wine64 \
 && rm -rf /var/lib/apt/lists/*
ENV WINEDEBUG=-all WINEPREFIX=/prefix WINEARCH=win64 HOME=/root
# Grab the official installer, extract with 7z (no GUI wizard needed)
RUN curl -fL --retry 5 --retry-all-errors -C - \
      -o /tmp/mt5setup.exe \
      https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe \
 && apt-get update && apt-get install -y --no-install-recommends p7zip-full \
 && mkdir -p "$WINEPREFIX/drive_c/Program Files/MetaTrader 5" \
 && 7z x -y -o"$WINEPREFIX/drive_c/Program Files/MetaTrader 5" /tmp/mt5setup.exe >/dev/null \
 && rm -f /tmp/mt5setup.exe \
 && rm -rf /var/lib/apt/lists/*
# Compile the EA (MetaEditor runs headless under wine)
COPY SpotDump.mq5 /src/SpotDump.mq5
RUN mkdir -p "/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Experts" \
 && cp /src/SpotDump.mq5 "/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Experts/" \
 && cd "/prefix/drive_c/Program Files/MetaTrader 5" \
 && wine64 metaeditor64.exe /compile:"C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\SpotDump.mq5" /log || true \
 && test -f "MQL5/Experts/SpotDump.ex5" \
    || { cat "MQL5/Experts/SpotDump.log" 2>/dev/null || true; echo "EA compile failed"; exit 1; }

FROM debian:bookworm-slim
RUN dpkg --print-foreign-architectures i386 2>/dev/null || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-dev build-essential \
      wine wine64 wine32:i386 \
      xvfb x11-utils \
      curl sqlite3 ca-certificates procps psmisc iproute2 tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -ms /bin/bash mt5

# Python deps into a venv at the same path the native install uses
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --prefer-binary \
      --upgrade pip wheel setuptools \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --prefer-binary \
      -r /app/requirements.txt

COPY --from=build /prefix/drive_c/Program\ Files/MetaTrader\ 5/ /mt5/master/
COPY --from=build /prefix/drive_c/Program\ Files/MetaTrader\ 5/MQL5/Experts/SpotDump.ex5 /mt5/SpotDump.ex5
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY . /app/
RUN chmod +x /app/*.py /app/run.sh 2>/dev/null; true

ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH \
    WINEPREFIX=/data/prefix WINEARCH=win64 WINEDEBUG=-all \
    DISPLAY=:99 MT5_WINEPREFIX=/data/prefix \
    MT5_WEB_HOST=0.0.0.0
VOLUME ["/data"]
EXPOSE 8000
WORKDIR /app
USER mt5

HEALTHCHECK --interval=30s --timeout=5s --start-retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["all"]
