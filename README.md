<div align="center">
  <img src="assets/logo.svg" alt="VAS logo" width="100"/>
  <h1>VAS — Versatile Autoregistration Server</h1>
</div>

[![en](https://img.shields.io/badge/lang-en-blue.svg)](README.md)
[![es](https://img.shields.io/badge/lang-es-green.svg)](README.es.md)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Debian package](https://img.shields.io/badge/package-versatile--autoreg--vas-brightgreen)](https://github.com/GabrielNavi/vas/releases)
[![Python 3](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey.svg)]()

Lightweight network inventory server for centrally managed Linux environments. Maintains the canonical registry of active, inactive, and archived machines through a minimalist REST API. Designed for educational networks with hundreds of clients.

> 📖 [Versión en español](README.es.md)

---

## Table of Contents

- [Ecosystem](#ecosystem)
- [Quick Start](#quick-start)
- [Installed Files](#installed-files)
- [REST API](#rest-api)
- [Client Lifecycle](#client-lifecycle)
- [Configuration](#configuration)
- [Push Notifications (Hooks)](#push-notifications-hooks)
- [Security](#security)
- [Service Management](#service-management)
- [Wiki](#wiki)
- [License](#license)

---

## Ecosystem

```
VAS  ←─ POST /register, /heartbeat ── VAC  (client, each machine)
VAS  ──→ bump hooks / UDP push      ──▶ VAL  (generic consumer with hooks)
VAS  ←─ federation                  ── VAF  (federated server, experimental)
```

| Package | Repository | Description |
|---------|------------|-------------|
| `versatile-autoreg-vas` | [vas](https://github.com/GabrielNavi/vas) ← *this* | Inventory server |
| `versatile-autoreg-vac` | vac | Autoregistration client |
| `versatile-autoreg-val` | val | Generic consumer with hooks |
| `versatile-autoreg-vaf` | vaf | Server federation (experimental) |

---

## Quick Start

```bash
# Install the Debian package
sudo dpkg -i versatile-autoreg-vas_*.deb
sudo apt-get -f install          # resolve dependencies if needed

# Configure (optional — all defaults are sensible)
sudo nano /etc/vas/vas.conf

# Start
sudo systemctl enable --now vas

# Verify
curl http://localhost:8000/health
```

> **Dependencies:** `python3-fastapi`, `uvicorn | python3-uvicorn`, `python3-pydantic`  
> See [Installation](../../wiki/Instalacion) in the wiki for full instructions.

---

## Installed Files

| Path | Description |
|------|-------------|
| `/usr/bin/vas` | Server launcher (uvicorn wrapper) |
| `/usr/bin/vas-cleanup` | Interactive CLI for manual lifecycle management |
| `/usr/lib/vas/vas.py` | FastAPI server: endpoints, config, lifecycle logic |
| `/usr/lib/vas/database.py` | SQLite layer: clients, version, fire-and-forget hooks |
| `/usr/lib/vas/vas_log.py` | Configurable logging (`LOG_LEVEL`, `LOG_FILE`) |
| `/etc/vas/vas.conf` | Main configuration file |
| `/etc/vas/vas.conf.d/` | Config overlays applied in lexical order |
| `/etc/vas/hooks.d/` | Scripts executed after each `bump_version` |
| `/usr/share/vas/vas.conf.defaults` | Exhaustive reference of all variables (read-only) |
| `/usr/share/vas/hooks.d.examples/val-local` | Example hook: UDP push to VAL-Aware instances |
| `/lib/systemd/system/vas.service` | systemd unit (runs as `vas` system user) |
| `/var/lib/vas/vas.db` | SQLite database (created on first start) |
| `/var/lib/vas/version` | Inventory version string (`YYYYMMDDHHMMSSmmm`) |

---

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check with no side-effects or logging |
| `GET` | `/version` | Current inventory version |
| `GET` | `/clients` | Client list; supports `?status=` and `?extra_key=` filters |
| `GET` | `/clients/{id}` | Single client by UUID |
| `POST` | `/register` | Register or update a client; returns `{status, version}` |
| `POST` | `/heartbeat` | Update `last_seen` without touching data; returns 404 if UUID unknown |

> The inventory version only increments when real data changes or a client transitions to `inactive`. Periodic heartbeats do not bump the version.

Full documentation: [wiki/API](../../wiki/API)

---

## Client Lifecycle

```
active ──TTL_INACTIVE──▶ inactive  (version bumped → consumers detect the drop)
       ──TTL_ARCHIVE───▶ archived  (historical record, not counted as active)
       ──TTL_PURGE─────▶ DELETE    (0d = keep forever)
```

- Any `POST /register` or `POST /heartbeat` reactivates an `inactive`/`archived` client.
- The lifecycle runs on **VAS startup** and every `LIFECYCLE_INTERVAL` (default: `24h`).
- Duration parser accepts `30d`, `12h`, `90m`, `60s`; bare integers assume days with a `[WARN]` log entry.

More details: [wiki/Ciclo-de-vida](../../wiki/Ciclo-de-vida)

---

## Configuration

```ini
# /etc/vas/vas.conf  (full reference at /usr/share/vas/vas.conf.defaults)

PORT=8000
TTL_INACTIVE=30d
TTL_ARCHIVE=90d
TTL_PURGE=365d
LIFECYCLE_INTERVAL=24h
LOG_LEVEL=normal        # no | normal | debug
# LOG_FILE=/var/log/vas/vas.log
HOOKS_DIR=/etc/vas/hooks.d
# HOOKS_LOG=/var/log/vas/hooks.log
```

Overlay files in `/etc/vas/vas.conf.d/*.conf` are applied on top of `vas.conf` in lexical order.

Full guide: [wiki/Configuracion](../../wiki/Configuracion)

---

## Push Notifications (Hooks)

After each `bump_version`, VAS launches all executable scripts in `HOOKS_DIR` in parallel (fire-and-forget). The bundled `val-local` hook sends a UDP datagram to every VAL-Aware instance that has published its endpoint in `extra_imperative.inform.url`, letting it react in milliseconds instead of waiting for the next polling cycle.

Hook output goes to journald alongside `[VAS]` log messages. Set `HOOKS_LOG` to redirect it to a separate file.

See also: [versatile-autoreg-hooks](https://github.com/GabrielNavi) — collection of example hooks for VAS, VAC and VAL.

---

## Security

VAS does not implement authentication on its REST API. **Do not expose the port to untrusted networks.**

- The security model assumes a closed management network (centrally managed classroom network).
- Runs as the `vas` system user (no shell, no writable home directory).
- The configuration parser does not execute code: it splits `key=value` pairs with quote stripping.
- `GET /clients` omits UUIDs from the public listing; only `GET /clients/{id}` exposes them.
- For authentication or HTTPS: place a reverse proxy (nginx, HAProxy) in front of VAS and set `VAS_SCHEME=https` on the clients.

---

## Service Management

```bash
sudo systemctl status vas
sudo systemctl restart vas
journalctl -u vas -f
journalctl -u vas | grep '\[LIFECYCLE\]'
journalctl -u vas | grep '\[ERROR\]'
```

---

## Wiki

[Installation](../../wiki/Instalacion) · [Configuration](../../wiki/Configuracion) · [API](../../wiki/API) · [Client Lifecycle](../../wiki/Ciclo-de-vida) · [Logging](../../wiki/Logging) · [Push Notifications](../../wiki/Push-notify)

---

## License

[Apache License 2.0](LICENSE)
