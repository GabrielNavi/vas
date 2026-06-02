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

Servidor de inventario de red ligero para entornos Linux gestionados centralmente. Mantiene el registro canónico de equipos activos, inactivos y archivados mediante una API REST minimalista. Diseñado para redes educativas con centenares de clientes.

---

## Tabla de contenidos

- [Ecosistema](#ecosistema)
- [Instalación rápida](#instalación-rápida)
- [Archivos instalados](#archivos-instalados)
- [API REST](#api-rest)
- [Ciclo de vida de clientes](#ciclo-de-vida-de-clientes)
- [Configuración](#configuración)
- [Notificación push (hooks)](#notificación-push-hooks)
- [Seguridad](#seguridad)
- [Servicio](#servicio)
- [Wiki](#wiki)
- [Licencia](#licencia)

---

## Ecosistema

```
VAS  ←─ POST /register, /heartbeat ── VAC  (cliente, cada equipo)
VAS  ──→ bump hooks / UDP push      ──▶ VAL  (consumidor con hooks)
VAS  ←─ federación                  ── VAF  (servidor federado, experimental)
```

| Paquete | Repositorio | Descripción |
|---------|-------------|-------------|
| `versatile-autoreg-vas` | [vas](https://github.com/GabrielNavi/vas) ← *este* | Servidor de inventario |
| `versatile-autoreg-vac` | vac | Cliente de autoregistro |
| `versatile-autoreg-val` | val | Consumidor genérico con hooks |
| `versatile-autoreg-vaf` | vaf | Federación de servidores (experimental) |

---

## Instalación rápida

```bash
# Instalar el paquete Debian
sudo dpkg -i versatile-autoreg-vas_*.deb
sudo apt-get -f install          # resolver dependencias si es necesario

# Configurar (mínimo necesario — todos los defaults son válidos)
sudo nano /etc/vas/vas.conf

# Arrancar
sudo systemctl enable --now vas

# Verificar
curl http://localhost:8000/health
```

> **Dependencias:** `python3-fastapi`, `uvicorn | python3-uvicorn`, `python3-pydantic`  
> Ver [Instalación](../../wiki/Instalacion) en la wiki para instrucciones completas.

---

## Archivos instalados

| Ruta | Descripción |
|------|-------------|
| `/usr/bin/vas` | Lanzador del servidor (uvicorn) |
| `/usr/bin/vas-cleanup` | Gestión manual interactiva del ciclo de vida |
| `/usr/lib/vas/vas.py` | Servidor FastAPI: endpoints, configuración, ciclo de vida |
| `/usr/lib/vas/database.py` | Capa SQLite: clientes, versión, hooks fire-and-forget |
| `/usr/lib/vas/vas_log.py` | Logging configurable (`LOG_LEVEL`, `LOG_FILE`) |
| `/etc/vas/vas.conf` | Configuración principal |
| `/etc/vas/vas.conf.d/` | Overlays en orden lexical |
| `/etc/vas/hooks.d/` | Scripts lanzados tras cada `bump_version` |
| `/usr/share/vas/vas.conf.defaults` | Referencia exhaustiva de todas las variables (solo lectura) |
| `/usr/share/vas/hooks.d.examples/val-local` | Hook de ejemplo: push UDP a instancias VAL-Aware |
| `/lib/systemd/system/vas.service` | Unidad systemd (corre como usuario `vas`) |
| `/var/lib/vas/vas.db` | Base de datos SQLite (creada al arrancar) |
| `/var/lib/vas/version` | Versión del inventario (`YYYYMMDDHHMMSSmmm`) |

---

## API REST

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/health` | Healthcheck sin side-effects ni log |
| `GET` | `/version` | Versión actual del inventario |
| `GET` | `/clients` | Lista de clientes; soporta `?status=` y `?extra_key=` |
| `GET` | `/clients/{id}` | Cliente individual por UUID |
| `POST` | `/register` | Registra o actualiza un cliente; devuelve `{status, version}` |
| `POST` | `/heartbeat` | Actualiza `last_seen` sin modificar datos; devuelve 404 si UUID desconocido |

> La versión del inventario solo sube cuando cambian datos reales o un cliente pasa a `inactive`. Los heartbeats periódicos no modifican la versión.

Documentación completa: [wiki/API](../../wiki/API)

---

## Ciclo de vida de clientes

```
active ──TTL_INACTIVE──▶ inactive  (sube versión → consumidores detectan la baja)
       ──TTL_ARCHIVE───▶ archived  (histórico, no cuenta como activo)
       ──TTL_PURGE─────▶ DELETE    (0d = conservar indefinidamente)
```

- Cualquier `POST /register` o `POST /heartbeat` reactiva un cliente `inactive`/`archived`.
- El ciclo se ejecuta al **arrancar VAS** y cada `LIFECYCLE_INTERVAL` (por defecto: `24h`).
- El parser de duraciones acepta `30d`, `12h`, `90m`, `60s`; sin sufijo asume días con `[WARN]`.

Más información: [wiki/Ciclo-de-vida](../../wiki/Ciclo-de-vida)

---

## Configuración

```ini
# /etc/vas/vas.conf  (referencia completa en /usr/share/vas/vas.conf.defaults)

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

Los overlays en `/etc/vas/vas.conf.d/*.conf` se aplican en orden lexical sobre `vas.conf`.

Guía completa: [wiki/Configuracion](../../wiki/Configuracion)

---

## Notificación push (hooks)

Tras cada `bump_version`, VAS lanza en paralelo (fire-and-forget) todos los scripts ejecutables de `HOOKS_DIR`. El hook incluido `val-local` envía un datagrama UDP a cada VAL-Aware configurado, permitiéndole reaccionar en milisegundos en lugar de esperar el siguiente ciclo de polling.

La salida de los hooks va a journald junto a los mensajes `[VAS]`. Con `HOOKS_LOG` se redirige a un fichero independiente.

Ver también: [versatile-autoreg-hooks](https://github.com/GabrielNavi) — colección de hooks de ejemplo para VAS, VAC y VAL.

---

## Seguridad

VAS no implementa autenticación en su API REST. **No exponer el puerto a redes no confiables.**

- El modelo de seguridad asume red de gestión cerrada (red de aula gestionada centralmente).
- Corre como usuario de sistema `vas` (sin shell, sin home escriturable).
- El parser de configuración no ejecuta código: divide `clave=valor` con strip de comillas.
- `GET /clients` omite el UUID del listado; solo `GET /clients/{id}` lo expone.
- Para autenticación o HTTPS: situar un proxy inverso (nginx, HAProxy) delante de VAS y configurar `VAS_SCHEME=https` en los clientes.

---

## Servicio

```bash
sudo systemctl status vas
sudo systemctl restart vas
journalctl -u vas -f
journalctl -u vas | grep '\[LIFECYCLE\]'
journalctl -u vas | grep '\[ERROR\]'
```

---

## Wiki

[Instalación](../../wiki/ES_Instalacion) · [Configuración](../../wiki/ES_Configuracion) · [API](../../wiki/ES_API) · [Ciclo de vida](../../wiki/ES_Ciclo-de-vida) · [Logging](../../wiki/ES_Logging) · [Notificación push](../../wiki/ES_Notficiacion)

---

## Licencia

[Apache License 2.0](LICENSE)
