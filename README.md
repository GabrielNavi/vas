# vx-dga-l-vas — Versatile Autoregistration Server

Servidor de inventario de red ligero. Mantiene el registro canónico de equipos activos, inactivos y archivados mediante una API REST minimalista. Diseñado para redes educativas con centenares de equipos Linux gestionados centralmente.

## Ecosistema

```
vx-dga-l-vas          → servidor de inventario (este paquete)
vx-dga-l-vac          → cliente de autoregistro (cada equipo)
vx-dga-l-val          → consumidor genérico con hooks
vx-dga-l-veyon-sync   → integración Veyon opcional (legacy)
```

## Requisitos

- `python3`, `python3-fastapi`, `uvicorn | python3-uvicorn`, `python3-pydantic`
- systemd

## Archivos instalados

| Ruta | Descripción |
|---|---|
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

## Estado en disco

| Ruta | Descripción |
|---|---|
| `/var/lib/vas/vas.db` | Base de datos SQLite |
| `/var/lib/vas/version` | Versión del inventario (`YYYYMMDDHHMMSSmmm`) |
| `/var/log/vas/` | Logs opcionales (`LOG_FILE`, `HOOKS_LOG`) |

## API REST

| Método | Endpoint | Descripción |
|---|---|---|
| `GET` | `/health` | Healthcheck sin side-effects ni log (proxies, monitorización) |
| `GET` | `/version` | Versión actual del inventario |
| `GET` | `/clients` | Clientes filtrados por `?status=` y/o `?extra_key=` |
| `GET` | `/clients/{id}` | Cliente individual por UUID |
| `POST` | `/register` | Registra o actualiza un cliente; retorna `{status, version}` |
| `POST` | `/heartbeat` | Actualiza `last_seen` sin tocar datos; retorna 404 si UUID desconocido |

La versión solo sube cuando cambian datos reales o un cliente pasa a `inactive`. Los heartbeats periódicos no modifican la versión.

## Ciclo de vida de clientes

```
active → inactive  (TTL_INACTIVE, sube versión → consumidores detectan la baja)
       → archived  (TTL_ARCHIVE, histórico)
       → DELETE    (TTL_PURGE; 0d = conservar para siempre)
```

Cualquier `POST /register` o `POST /heartbeat` reactiva un cliente `inactive`/`archived` automáticamente.

El ciclo se ejecuta al **arrancar VAS** y cada `LIFECYCLE_INTERVAL` (defecto: `24h`). El parser de duraciones acepta `30d`, `12h`, `90m`, `60s`; sin sufijo asume días con `[WARN]` en log.

## Configuración

```ini
# /etc/vas/vas.conf  (referencia completa en vas.conf.defaults)
PORT=8000
TTL_INACTIVE=30d
TTL_ARCHIVE=90d
TTL_PURGE=365d
LIFECYCLE_INTERVAL=24h
LOG_LEVEL=normal
# LOG_FILE=/var/log/vas/vas.log
HOOKS_DIR=/etc/vas/hooks.d
# HOOKS_LOG=/var/log/vas/hooks.log
```

## Notificación push (hooks)

Tras cada `bump_version`, VAS lanza en paralelo (fire-and-forget) todos los scripts ejecutables de `HOOKS_DIR`. En instalación fresca, el hook `val-local` se activa automáticamente: envía un datagrama UDP a cada equipo que haya publicado su endpoint en `extra_imperative.inform.url`, permitiendo que VAL-Aware reaccione en milisegundos.

La salida de los hooks va a journald por defecto (junto a los mensajes `[VAS]`). Con `HOOKS_LOG` se redirige a un fichero independiente.

## Seguridad

- Corre como usuario de sistema `vas` (sin shell, sin home).
- El parser de configuración no ejecuta código: divide `clave=valor` + strip de comillas.
- `GET /clients` omite el UUID del listado público; solo `GET /clients/{id}` lo expone.

## Servicio

```bash
systemctl status vas
systemctl restart vas
journalctl -u vas -f
journalctl -u vas | grep '\[LIFECYCLE\]'
journalctl -u vas | grep '\[ERROR\]'
```

## Wiki

[Instalación](../../wiki/Instalacion) · [Configuración](../../wiki/Configuracion) · [API](../../wiki/API) · [Ciclo de vida](../../wiki/Ciclo-de-vida) · [Logging](../../wiki/Logging) · [Notificación push](../../wiki/Push-notify)
