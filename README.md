# MT5 Dual-Terminal Algo Trading Bridge

An algorithmic trading stack that runs two MetaTrader 5 terminals side by
side under Wine and drives them entirely from Python through a file-based
execution channel. It runs natively on Linux or in the bundled container
on any OS (Windows/macOS included). Built and tested against HFM demo
accounts and the 24/7 gold symbol `XAUUSD247`.

One command starts everything; Ctrl+C stops everything.

## Quick start

Two ways in - pick whichever fits the computer.

**A. Docker or Podman (any OS - the easiest, nothing else to install but
the container runtime)**

```bash
git clone https://github.com/Tony46117/mt5.git && cd mt5
docker compose up --build        # or: make docker
```

The first boot builds the image and installs MT5 into the `mt5-data`
volume (~2-5 min); later boots start in seconds. The web panel is then on
http://localhost:8000. `make docker` also writes a `.env` with a random
`MT5_MACHINE_KEY` so stored logins survive container recreation.

**B. Linux native (Wine - lowest latency)**

```bash
bash install.sh      # one-time: packages, Wine, MT5, venv, EA  (or: make setup)
bash run.sh          # start everything                          (or: make run)
```

`run.sh` uses the venv `install.sh` created and will create/repair it if
it is missing, so on a machine that already has Wine, `bash run.sh` is
usually all you need. Ctrl+C stops the bridge, the web app and both
terminals; `bash run.sh stop` (or `make stop`) does the same from another
shell.

**Then, both paths:** open http://localhost:8000, log into your accounts
from the panel (hot-adopted within a second), or seed `acc.env` first -
see [Configure accounts](#configure-accounts). `make help` lists every
shortcut (`setup`, `run`, `stop`, `docker`, `logs`, `shell`, `down`,
`wipe`).

## What it does

- Boots two MT5 terminals concurrently, auto-logged into the stored
  accounts, algo trading enforced ON, each terminal restored to exactly
  one EURUSD H1 chart (no window flood).
- Verifies each terminal's EA-reported login against the stored session
  and auto-heals stale feeds, detached EAs and dead terminals.
- Hot account switching: log into any account on any terminal (MT5 UI)
  and the whole software adopts it within about a second - executor
  routing, web panel, monitor, close, metrics. Conversely, if the
  stored session is rewritten externally, terminals are reconnected
  into it.
- Full quote-universe probing: the EA publishes every symbol the
  account can see (broker-suffixed pairs, cent variants, Deriv's
  Boom/Crash/Jump/Volatility indices) to `symbols.csv`, and
  `broker_prober` reports that catalogue in milliseconds - no per-symbol
  round trips, both accounts at once. Symbols are subscribed before
  MT5 is asked for a price, so a synthetic index that had never been
  selected still shows up (and stays tradeable) as soon as it quotes.
- Order batches drain in one terminal tick: command files are written
  atomically and the EA consumes every pending one per pass, so a
  50-position basket is handed to the broker in ~30 ms instead of ~50
  pointer handshakes. Measured channel cost is ~0.6 ms per command;
  what remains is the broker's own fill time. Failed logins surface the
  trade server's own rejection instead of looking like a slow sync.
- Millisecond scheduled trading: a background scheduler fires each
  schedule slot to the planned second with atomic slot claims (no
  double fires, no lost slots), concurrent per-terminal order batches,
  500 ms automatic retry on failed opens/closes, and automatic
  open-then-close lifetimes.
- Web trading panel on port 8000: buy/sell buttons execute directly
  through the exec channel with no database work on the hot path.
- Credential hygiene: start-config login blocks are written 0600 and
  scrubbed once the boot has consumed them, and the session file is
  obfuscated with 0600 permissions. Terminal launches carry a small
  random jitter so the two logins are not machine-simultaneous. (This is
  housekeeping, not anonymity - MT5 itself stores the account in its own
  config, and your broker sees both logins regardless.)

## Architecture

| Component       | Role |
|-----------------|------|
| `SpotDump.mq5`  | MQL5 Expert Advisor attached to each terminal's chart. Writes the market feed, account header and the full broker symbol catalogue (`symbols.csv`) to `MQL5/Files`, consumes order commands (`OPEN`, `PENDING`, `STOPEXE`/`STOPEXESL`, `MODIFY`, `CLOSE`, `CLOSEALL`, `PING`, `PROBE`) from `exec_in.<id>.txt` - every pending file it finds each tick, pointer file first - and appends results to `exec_out.csv`. |
| `spot.py`       | Terminal lifecycle (launch, stop, restart, minimal one-chart boot), start-config generation and credential scrubbing, feed readers. |
| `bridge.py`     | Supervisor screen. Concurrent boot of both terminals, feed/watchdog auto-heal, hot account switching, antidetect scrubbing. |
| `executor.py`   | The only way Python trades. `SendCommand` file protocol, batched concurrent orders, `FutureTradeScheduler` with CAS claims, fire-latency control, 500 ms retries. |
| `app.py`        | Flask web panel (port 8000): manual trading buttons, pending-order ticket, schedule management, health endpoint. Also runs the scheduler. The app never places a trade on its own - orders only ever come from an explicit click or a schedule. |
| `close.py`      | Database-free concurrent close of all positions (one atomic `CLOSEALL` per terminal). |
| `monitor.py`    | Dual-account live dashboard: open positions only, strict identity checks, no event log. |
| `session.py`    | Obfuscated credential store (`session.json`), stat-validated on every read so account changes propagate live across all processes. |
| `config.py`     | Paths, tunables and the access-point registry (`access_points.env` + `MT5_AP_*` env + the auto-learned `access_points.json`). |
| `account_engine.py` | Account Integration Engine: concurrent per-terminal login/switch/adopt/logout of any account (live or demo) with pre-validation, broker-verified results (a login only counts once the trade server authorized it, not when the EA merely echoes the number), demo/live detection, access-point registry (`--access-point`, `--servers`), an operation registry, CLI (`--login/--adopt/--status/...`) and `/api/engine/*` endpoints for the panel. |
| `database.py`   | SQLite/Postgres storage for schedules and the fired log. |
| `broker_prober.py` | Probes the account's full quote universe and order-fill behaviour. The EA writes every symbol the account can see to `symbols.csv`, so the prober reports the whole broker catalogue (including Deriv's Boom/Crash/Jump/Volatility indices and any symbol a broker renames with extra letters, e.g. `XAUUSD247`, `EURUSD.Z`) with no exec round trips, then only PROBEs what the catalogue is missing. Both terminals probe concurrently. |
| `metrics.py`    | Closed-trade and equity observation. |
| `run.sh`        | One-command launcher for the whole stack with full teardown on Ctrl+C. |
| `install.sh`    | System bootstrap: packages, Wine prefix, MT5 install, venv, EA compilation. |
| `Makefile`      | One-line shortcuts for setup/run/stop/docker/logs/shell - `make help` lists them. |
| `front.py`      | Server-rendered HTML/CSS/JS for the dashboard and the trading panel. |
| `info.py`       | One-shot account/spread report for the logged-in accounts. |
| `ms.py`         | Standalone terminal candlestick chart (not used by the stack). |
| `make_bridge_tpl.py` | Installs the `Bridge.tpl` chart template into both terminals (run by `install.sh`). |

## Requirements

- **Docker or Podman** (any OS), or **Linux with Wine 9+** (esync/fsync
  capable builds are used when present) for native runs
- **Python 3.10+** for native runs; `run.sh` creates and maintains the
  `~/python312` venv automatically, so no manual pip setup is needed
- **x86_64 CPU** - Wine and MT5 are 64-bit Intel/AMD. On Apple Silicon,
  Docker runs the amd64 image through emulation (works, slower).
- An MT5 broker account pair (demo recommended). HFM demo accounts with
  `XAUUSD247` are the tested configuration.

## Install (Linux native)

```bash
bash install.sh        # or: make setup
```

Handles packages, the Wine prefix, the MetaTrader 5 install, the Python
virtualenv and EA compilation, with retries and non-fatal fallbacks on
interactive steps (an MT5 GUI wizard may appear once - click through it).
Everything is skipped when already present, so it is safe to re-run after
an interruption. When it finishes, `bash run.sh` starts the stack.

## Run with Docker (any OS, including Windows)

The container bundles Wine + Xvfb + the pre-compiled EA. It auto-detects the
environment (plain Docker, WSL2, Kubernetes) and the X display (X11 forward,
Wayland, or a private headless Xvfb) on every start - no flags needed.

```bash
docker compose up --build       # or: make docker
```

- Web panel: http://localhost:8000
- Persistent state (wine prefix, MT5 installs, databases, logs) lives in the
  `mt5-data` volume; `logs/` inside the container maps to `/data/logs`.
- First boot installs MT5 into the volume and creates terminal 2 (~2 min),
  later boots start in seconds.
- `make docker` creates `.env` (random `MT5_MACHINE_KEY`, gitignored) and
  `./data/` for you on first run. If you start with plain
  `docker compose up --build`, copy `.env.example` to `.env` once. Keep
  `MT5_MACHINE_KEY` stable or the stored session cannot be decrypted.
- Different host port: `MT5_WEB_PORT=8100 docker compose up --build`.

**Apple Silicon Macs:** the image is `linux/amd64` because Wine + MT5 are
x86_64. Docker Desktop runs it through Rosetta/QEMU emulation - enable
"Use Rosetta for x86/amd64 emulation" for a usable speed, and expect the
first build to take a few minutes longer.

**Windows:** install Docker Desktop (WSL2 backend), then from the project
folder run `bash run.sh` in Git Bash / WSL - it detects the OS and hands
everything to the container stack. All the Wine/MT5 logic stays in Linux
containers, so the stack behaves identically to a native Linux box.

**Podman (preferred on Fedora/RHEL Linux):** no changes needed - `run.sh`
prefers podman when wine is absent, and rootless podman works out of the box
(`podman run` flags used: `--ipc=host` for wine's esync, `:Z` for SELinux).
Compose users can add the override file:

```bash
podman build -t mt5-bridge:latest .
podman run -d --name mt5-bridge -p 8000:8000 --ipc=host \
  -v mt5-data:/data:Z -v "$PWD/data:/data/seed:ro,Z" \
  -e MT5_MACHINE_KEY=change-me localhost/mt5-bridge:latest all
```

On hosts without unprivileged user namespaces (CentOS/RHEL defaults) the
container skips `wineboot` and reuses the wineprefix baked into the image.
If MetaTrader 5 cannot be fetched at runtime (slow/quota-limited links), copy
a working install to `./data/mt5-master/` (must contain `terminal64.exe`) and
it is seeded on next boot.

**Accounts:** put your `acc.env` (see `acc.env.example`) at `./data/acc.env`
on the host - the container copies it in and seeds the obfuscated session on
first boot. Set `MT5_MACHINE_KEY` in the environment (any long random string)
so the encrypted `session.json` survives container recreation. You can also
log in live from the web panel.

Useful commands (all have `make` equivalents - `make help`):

```bash
docker compose up --build -d          # detached          (make docker-up)
docker compose logs -f                # follow logs       (make logs)
docker compose exec mt5-bridge bash   # shell inside      (make shell)
docker compose down                   # stop, volume kept (make down)
docker compose down -v                # stop AND wipe state (make wipe)
```

## Configure accounts

Seed the stored session once (prompts per terminal, password input does
not echo):

```bash
python session.py
```

`session.json` (obfuscated, 0600) is what the bridge actually boots from.
`python session.py --status` shows who is stored, `--logout` forgets it.
(`--seed` imports credentials from `acc.env` - copy `acc.env.example` to
`acc.env`, fill in logins, then `python session.py --seed`. For the Docker
stack put the file at `./data/acc.env` instead.)

The obfuscation is XOR against a machine-derived key - it keeps passwords
out of plaintext files and backups, it is **not** encryption against
anyone with read access to your account. During boot the password is also
written into the terminal's start config (0600) and scrubbed once the feed
is live.

### Incorporating a new broker (access points)

MT5 logs in by trade-server NAME, and it only dials a name that is already
in its own server list. A broker this install has never connected to (a
brand-new account, a second broker alongside your existing one) cannot be
reached by name alone: MT5 adopts the login into its config - so the EA
header even echoes it back - but never opens a connection and never writes
a single line to its journal. That is the "login takes forever and then
says the password is wrong" symptom.

The fix is to hand MT5 the server's **access point** (host:port) once. Add
it to `access_points.env` in this directory:

```
# server name exactly as the broker writes it = host:port
CXMDirect-Live = mt5.cxmdirect.com:443
```

or set `MT5_AP_<SERVERNAME>=host:port` (non-alphanumerics become `_`, e.g.
`MT5_AP_CXMDIRECT_LIVE=...`), or use the panel's **ADD ACCESS POINT**
button / the CLI:

```bash
python account_engine.py --access-point CXMDirect-Live mt5.cxmdirect.com:443
python account_engine.py --servers     # what MT5 can already reach + every AP
```

After one successful connect MT5 knows the server itself, the access point
is remembered automatically (`access_points.json`), and plain name logins
keep working. Logging in once by hand inside MT5 (File -> Login to Trade
Account) and pressing **ADOPT** has the same effect.

A wrong or unreachable server no longer costs a 90 s wait that blames the
password: the engine detects "MT5 never attempted this login" in seconds
and tells you exactly which access point to add.

## Run

```bash
bash run.sh        # or: make run
```

- Starts the web panel on `http://127.0.0.1:8000` and the bridge
  supervisor (which boots both terminals concurrently).
- The supervisor screen is the live status dashboard.
- The venv is created/repaired automatically (at `~/python312`) and missing
  libraries are installed on the first run.
- `Ctrl+C` stops the bridge, the web app and both MT5 terminals.

Other commands:

```bash
bash run.sh stop                          # tear everything down (make stop)
MT5_RUN_KEEP_TERMINALS=1 bash run.sh      # keep terminals on exit
```

Do not run `run.sh` with sudo; it must run as your normal user so Wine,
paths and file ownership stay consistent.

## Trading

- Web panel: `http://127.0.0.1:8000` - buy/sell buttons, schedules,
  account overview.
- Factory HUD: `/factory` (full-weight sidebar item just below
  **Scheduled Trades**) renders the
  factory.py news globe and LED ticker in the browser. The ms/us clock and
  the dotted globe are painted locally every frame (no per-frame server
  round-trip, so rotation/hover stay smooth); `/api/factory/map` serves the
  Natural Earth geometry once at 5.3x factory.py's dot density (~200k dots)
  and `/api/factory/news` the cached feed.
- Scheduled trades are stored with an execution time and a close time.
  Those times are entered in the **machine-local clock** (the same clock
  shown in the panel top bar and the schedule dials) and stored as UTC -
  the panel labels the fields and shows your offset, and `/api/clock`
  reports both. The
  executor fires opens to the planned second (commands are queued
  slightly ahead so fills land on the second), then closes them
  automatically. 24/7 symbols (anything with `247`, Boom/Crash, Deriv
  synthetics) fire on weekends without extra configuration; other
  symbols default to Monday-Friday and can be overridden with
  `MT5_TRADING_DAYS=0,1,2,3,4,5,6`.
- Both BUY and SELL can be scheduled. Symbol names are passed through
  case-sensitively, so brokers whose symbols are not upper-case
  (`Boom 1000 Index`) work as typed.
- CLI:

```bash
python executor.py --ping                          # heartbeat both terminals
python executor.py --open 1 XAUUSD247 BUY 0.01     # manual order
python executor.py --closeall 2 ALL                # close one account
python executor.py --status                        # schedules + fired log
python close.py                                    # close everything, concurrently
python close.py --pair XAUUSD247                   # close one symbol everywhere
python monitor.py                                  # live dual-account book
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MT5_TRADING_DAYS` | `0,1,2,3,4` | Weekdays (0=Mon) schedules may fire on |
| `MT5_MAX_PROBE_SYMBOLS` | `2000` | Ceiling on symbols the prober reports per account |
| `MT5_STRICT_UNKNOWN_SERVER` | unset | Set to `1` to always refuse a login to a trade server this box has never seen, even with an access point |
| `MT5_TRY_UNKNOWN_SERVER` | unset | Set to `1` to force the old attempt-by-name behaviour for a server no install has ever seen (default: fail fast with the access-point fix) |
| `MT5_SYMBOL_CAT_MAX_AGE_S` | `180` | Age after which a terminal's `symbols.csv` is no longer trusted as that account's quote universe |
| `MT5_CLOSE_VERIFY_TIMEOUT` | `15` | Seconds a close waits for the broker to report the position book empty |
| `MT5_IGNORE_AUTH_FAILURE` | unset | Set to `1` to ignore `authorization ... failed` lines from MT5's journal during a login |
| `MT5_NO_LAUNCH_JITTER` | unset | Set to `1` to disable anti-detect launch jitter |
| `MT5_RUN_KEEP_TERMINALS` | unset | Set to `1` so `run.sh` Ctrl+C keeps terminals running |
| `MT5_CORS_ORIGIN` | unset | Exact origin allowed to call the API cross-origin (see Security) |
| `MT5_WEB_HOST` / `MT5_WEB_PORT` | `127.0.0.1` / `8000` | Web panel bind address |
| `DATABASE_URL` | unset | PostgreSQL DSN; SQLite (`trades.db`) is used when unset |

## Security

The web panel has **no authentication**. Every `/api/trade`, `/api/close`
and `/api/schedule` call moves real money, so:

- keep it bound to `127.0.0.1` (the default) and do not port-forward it;
- anyone with a shell on the machine can trade your accounts;
- cross-origin requests are refused. Set `MT5_CORS_ORIGIN` to one exact
  origin only if an external dashboard genuinely needs it - never `*`.

Starting `app.py` never places a trade by itself: every order originates
from an explicit panel click or a schedule.

## Troubleshooting

- **Terminal ping fails but the feed is live** - the EA is detached from
  the exec channel; the supervisor auto-heals after its cooldown, or
  force it with `python monitor.py --restart 1`.
- **First order after a boot is slow** - the first order on a symbol can
  hit the broker's cold-symbol sync once; there is no startup warm-up
  trade (orders are only ever fired by a click or a schedule).
- **Web port busy** - another `app.py` instance is running; `bash run.sh stop`
  clears it. Docker users: remap with `MT5_WEB_PORT=8100 docker compose up`.
- **Double-boot race** - resolved automatically: `run.sh` clears any
  still-running terminals before the bridge boots its own.
- **`neither Wine nor Docker/Podman was found`** - install Docker (easiest)
  or run `bash install.sh` for the native Wine stack.
- **`could not create a venv` / `python3-venv` missing** - `sudo apt-get
  install python3-venv` (Debian/Ubuntu), then `bash run.sh` again.
- **`docker compose up` cannot build on Apple Silicon** - allow the amd64
  emulation (Docker Desktop -> Settings -> General -> "Use Rosetta for
  x86/amd64 emulation") and retry; the first build is slowest.
- **Docker container starts but the panel shows no session** - keep
  `MT5_MACHINE_KEY` the same as the previous run (`.env`); a changed key
  cannot decrypt `session.json`. Log in again from the panel or restore
  the old key.
- **Start over cleanly** - native: `bash run.sh stop` then delete
  `session.json`, `trades.db*`; Docker: `make wipe` (deletes the
  `mt5-data` volume and everything in it).

## CI / CD

`.github/workflows/ci.yml` runs on every push and pull request:

- **render** installs `requirements.txt`, byte-compiles every module, then
  boots `app.py` the way `run.sh` does (waitress, throwaway
  `MT5_MACHINE_KEY`) and requires `/`, `/panel`, `/scheduled`, `/factory`
  and `/api/{dashboard,panel,spots,accounts}` to render. The panel must
  also carry the current StopExe bracket and its button. The same check
  runs locally: `python3 ci_smoke.py --port 8010`.
- **publish** (main only, after `render`) builds the Dockerfile and
  pushes `ghcr.io/<owner>/<repo>:latest` plus a short-sha tag, so the
  image that ships is the commit that passed the render checks.

No Wine, MT5 or broker feed is needed for the checks - health reports
`degraded` off-box and that is accepted. The check refuses to run when
its port is already served by another process, so it can never pass
against a stale instance.

```bash
docker pull ghcr.io/tony46117/mt5:latest   # GHCR names are lowercase
docker run --rm -p 8000:8000 -e MT5_MACHINE_KEY=... ghcr.io/tony46117/mt5:latest
```

## Repository scope

Runtime code only. Credentials (`acc.env`, `session.json`, `.env`),
databases and run logs are intentionally kept out of version control;
`.env.example` and `acc.env.example` are tracked so a fresh clone knows
exactly what to copy. The CI pipeline itself (`.github/workflows/ci.yml`
and `ci_smoke.py`) is versioned with the code it checks.
