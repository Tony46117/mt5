# MT5 Dual-Terminal Algo Trading Bridge

A Linux-native algorithmic trading stack that runs two MetaTrader 5
terminals side by side under Wine and drives them entirely from Python
through a file-based execution channel. Built and tested against HFM
demo accounts and the 24/7 gold symbol `XAUUSD247`.

One command starts everything; Ctrl+C stops everything.

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
- Millisecond scheduled trading: a background scheduler fires each
  schedule slot to the planned second with atomic slot claims (no
  double fires, no lost slots), concurrent per-terminal order batches,
  500 ms automatic retry on failed opens/closes, and automatic
  open-then-close lifetimes.
- Web trading panel on port 8000: buy/sell buttons execute directly
  through the exec channel with no database work on the hot path.
- Antidetect hygiene: start-config login blocks are scrubbed after
  every boot consumed them, launch jitter de-syncs the two terminals'
  logins, the session file is obfuscated with 0600 permissions.

## Architecture

| Component       | Role |
|-----------------|------|
| `SpotDump.mq5`  | MQL5 Expert Advisor attached to each terminal's chart. Writes the market feed and account header to `MQL5/Files`, consumes order commands (`OPEN`, `CLOSE`, `CLOSEALL`, `PING`) from `exec_in.<id>.txt` via an atomic pointer file, appends results to `exec_out.csv`. |
| `spot.py`       | Terminal lifecycle (launch, stop, restart, minimal one-chart boot), start-config generation and credential scrubbing, feed readers. |
| `bridge.py`     | Supervisor screen. Concurrent boot of both terminals, feed/watchdog auto-heal, hot account switching, antidetect scrubbing. |
| `executor.py`   | The only way Python trades. `SendCommand` file protocol, batched concurrent orders, `FutureTradeScheduler` with CAS claims, fire-latency control, 500 ms retries. |
| `app.py`        | Flask web panel (port 8000): manual trading buttons, schedule management, health endpoint. Also runs the scheduler and a deploy warm-up trade per terminal so the first scheduled order never hits the broker's cold-symbol path. |
| `close.py`      | Database-free concurrent close of all positions (one atomic `CLOSEALL` per terminal). |
| `monitor.py`    | Dual-account live dashboard: open positions only, strict identity checks, no event log. |
| `session.py`    | Obfuscated credential store (`session.json`), stat-validated on every read so account changes propagate live across all processes. |
| `database.py`   | SQLite/Postgres storage for schedules and the fired log. |
| `broker_prober.py` | Probes broker symbols/spreads and order-fill behaviour. |
| `metrics.py`    | Closed-trade and equity observation. |
| `run.sh`        | One-command launcher for the whole stack with full teardown on Ctrl+C. |
| `install.sh`    | System bootstrap: packages, Wine prefix, MT5 install, venv, EA compilation. |

## Requirements

- Linux with Wine 9+ (esync/fsync capable builds are used when present)
- Python 3.12 (a venv at `~/python312` is used by `run.sh` when present)
- An MT5 broker account pair (demo recommended). HFM demo accounts with
  `XAUUSD247` are the tested configuration.

## Install

```bash
bash install.sh
```

Handles packages, the Wine prefix, the MetaTrader 5 install, the Python
virtualenv and EA compilation, with retries and non-fatal fallbacks on
interactive steps.

## Run with Docker (any OS, including Windows)

The container bundles Wine + Xvfb + the pre-compiled EA. It auto-detects the
environment (plain Docker, WSL2, Kubernetes) and the X display (X11 forward,
Wayland, or a private headless Xvfb) on every start - no flags needed.

```bash
docker compose up --build       # or: bash run.sh on Windows (auto-detects)
```

- Web panel: http://localhost:8000
- Persistent state (wine prefix, MT5 installs, databases, logs) lives in the
  `mt5-data` volume; `logs/` inside the container maps to `/data/logs`.
- First boot installs MT5 into the volume and creates terminal 2 (~2 min),
  later boots start in seconds.

**Windows:** install Docker Desktop (WSL2 backend), then from the project
folder run `bash run.sh` in Git Bash / WSL - it detects the OS and hands
everything to the container stack. All the Wine/MT5 logic stays in Linux
containers, so the stack behaves identically to a native Linux box.

**Accounts:** put your `acc.env` at `./data/acc.env` on the host - the
container copies it in and seeds the obfuscated session on first boot. Set
`MT5_MACHINE_KEY` in the environment (any long random string) so the
encrypted `session.json` survives container recreation. You can also log in
live from the web panel.

Useful commands:

```bash
docker compose up --build -d          # detached
docker compose logs -f                # follow logs
docker compose exec mt5-bridge bash   # shell inside
docker compose down                   # stop (volume persists)
docker compose down -v                # stop AND wipe all state
```

## Configure accounts

Seed the stored session once (prompts per terminal, password input does
not echo):

```bash
python session.py --seed
```

`acc.env` is supported as the credential source; `session.json` (obfuscated,
0600) is what the bridge actually boots from. Credentials are never left
in plaintext start configs while terminals run.

## Run

```bash
bash run.sh
```

- Starts the web panel on `http://127.0.0.1:8000` and the bridge
  supervisor (which boots both terminals concurrently).
- The supervisor screen is the live status dashboard.
- `Ctrl+C` stops the bridge, the web app and both MT5 terminals.

Other commands:

```bash
bash run.sh stop                          # tear everything down
MT5_RUN_KEEP_TERMINALS=1 bash run.sh      # keep terminals on exit
```

Do not run `run.sh` with sudo; it must run as your normal user so Wine,
paths and file ownership stay consistent.

## Trading

- Web panel: `http://127.0.0.1:8000` - buy/sell buttons, schedules,
  account overview.
- Scheduled trades are stored with an execution time and a close time;
  the executor fires opens to the planned second (commands are queued
  slightly ahead so fills land on the second), then closes them
  automatically. 24/7 symbols (anything with `247`, Boom/Crash, Deriv
  synthetics) fire on weekends without extra configuration; other
  symbols default to Monday-Friday and can be overridden with
  `MT5_TRADING_DAYS=0,1,2,3,4,5,6`.
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
| `MT5_NO_LAUNCH_JITTER` | unset | Set to `1` to disable anti-detect launch jitter |
| `MT5_RUN_KEEP_TERMINALS` | unset | Set to `1` so `run.sh` Ctrl+C keeps terminals running |

## Troubleshooting

- **Terminal ping fails but the feed is live** - the EA is detached from
  the exec channel; the supervisor auto-heals after its cooldown, or
  force it with `python monitor.py --restart 1`.
- **First order after a boot is slow** - the app performs a warm-up
  trade per terminal at startup; if a terminal was restarted manually,
  the first order may hit the broker's cold-symbol sync once.
- **Web port busy** - another `app.py` instance is running; `bash run.sh stop`
  clears it.
- **Double-boot race** - resolved automatically: `run.sh` clears any
  still-running terminals before the bridge boots its own.

## Repository scope

Runtime code only. Credentials (`acc.env`, `session.json`), databases,
logs and probe/CI tooling are intentionally kept out of version control.
