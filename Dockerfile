
FROM debian:bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wine wine64 xvfb p7zip-full \
 && rm -rf /var/lib/apt/lists/*
ENV WINEDEBUG=-all WINEPREFIX=/prefix WINEARCH=win64 HOME=/root \
    MT5DIR="/prefix/drive_c/Program Files/MetaTrader 5"

RUN curl -fL --retry 5 --retry-all-errors -C - \
      -o /tmp/mt5setup.exe \
      https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe

RUN timeout 200 xvfb-run -a wineboot -i >/dev/null 2>&1 \
    || timeout 60 xvfb-run -a wineboot >/dev/null 2>&1 || true
RUN wineserver -w 2>/dev/null || true

RUN mkdir -p "$MT5DIR" \
 && (7z x -y -o"$MT5DIR" /tmp/mt5setup.exe >/dev/null 2>&1 || true) \
 && if [ ! -f "$MT5DIR/terminal64.exe" ]; then \
      echo "no terminal64.exe after extract - trying silent install (5 min cap)"; \
      timeout 300 xvfb-run -a wine64 /tmp/mt5setup.exe /auto >/dev/null 2>&1 || true; \
      wineserver -w 2>/dev/null || true; \
    fi; \
    if [ -f "$MT5DIR/terminal64.exe" ]; then echo "MT5 binaries present"; \
    else echo "WARNING: MT5 not extracted at build time - runtime will install it"; fi

RUN test -d "$MT5DIR" && cp -a "$MT5DIR" /mt5-master || mkdir -p /mt5-master

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

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv \
      wine64 \
      xvfb x11-utils p7zip-full \
      curl sqlite3 ca-certificates procps psmisc iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -ms /bin/bash mt5

RUN ln -sf /usr/lib/wine/wine64 /usr/bin/wine64 \
 && ln -sf /usr/lib/wine/wine64 /usr/bin/wine \
 && ln -sf /usr/lib/wine/wineserver /usr/bin/wineserver

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --prefer-binary \
      --upgrade pip wheel setuptools \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --prefer-binary \
      -r /app/requirements.txt

COPY --from=build /prefix/ /mt5/prefix/
COPY --from=build /mt5-master/ /mt5/master/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY . /app/

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
