# vx-dga-l-vas — Vitalinux Autoregistration Server

Paquete Debian para Vitalinux que instala el servidor de inventario de red (VAS).

## Descripción

VAS mantiene un registro en tiempo real de los equipos presentes en la red: UUID persistente, hostname, IP, MAC y última vez visto (`last_seen`). Expone una API REST consumible por cualquier servicio: clientes VAC, sincronizadores Veyon, sistemas LDAP, herramientas de monitorización, etc.

La integración con Veyon es **opcional y externa**: el paquete `vx-dga-l-veyon-sync` actúa como consumidor independiente del registro, sin que VAS conozca ni dependa de Veyon.

## Ecosistema

```
vx-dga-l-vas          → registro canónico (este paquete)
vx-dga-l-vac          → cliente de autoregistro (cada equipo)
vx-dga-l-veyon-sync   → integración Veyon opcional
```

## Requisitos

- Python 3 con `python3-fastapi`, `python3-uvicorn`, `python3-pydantic`
- systemd

## Información del paquete

- Nombre: `vx-dga-l-vas`
- Versión: 0.9-8~rc
- Arquitectura: all
- Mantenedor: Gabriel Navia \<correos@gabrielnav.es\>
- Licencia: Apache 2.0

## Archivos instalados

| Ruta | Descripción |
|---|---|
| `usr/lib/vas/vas.py` | Aplicación FastAPI principal |
| `usr/lib/vas/database.py` | Capa de persistencia SQLite |
| `usr/lib/vas/vas_log.py` | Funciones de logging compartidas (log, log_debug) |
| `usr/bin/vas` | Wrapper de arranque (extrae PORT, lanza uvicorn) |
| `usr/bin/vas-cleanup` | Herramienta interactiva de limpieza manual |
| `etc/vas/vas.conf` | Configuración editable |
| `etc/vas/hooks.d/` | Directorio de hooks de `bump_version` (creado vacío) |
| `lib/systemd/system/vas.service` | Unidad systemd |
| `usr/share/vas/vas.conf.defaults` | Referencia de valores por defecto (solo lectura) |
| `usr/share/vas/hooks.d.examples/vcd-local` | Hook de ejemplo: notificación UDP a instancias VCD-Aware |

## API

| Método | Endpoint | Descripción |
|---|---|---|
| `POST` | `/register` | Registra o actualiza un cliente. Retorna `{status, version}`. |
| `POST` | `/heartbeat` | Actualiza `last_seen`. Sube versión si el cliente era `inactive`/`archived` (reactivación). 404 si UUID desconocido. |
| `GET` | `/version` | Versión actual del registro (`YYYYMMDDHHMMSSmmm`). |
| `GET` | `/clients` | Clientes filtrados por `?status=` y/o `?extra_key=` (default: `active`). |
| `GET` | `/clients/{id}` | Cliente individual por UUID. 404 si no existe. |

La versión solo se incrementa cuando cambian datos reales de algún cliente o cuando un cliente pasa a `inactive`. Los heartbeats periódicos actualizan `last_seen` sin modificar la versión.

`GET /clients` acepta `?extra_key=<clave>` para filtrar clientes que tengan esa clave en `extra_imperative` o `extra_informative`. Combinable con `?status=`. La respuesta incluye los campos extra completos; el consumidor decide qué hacer con ellos.

### Semántica de campos extra

`POST /register` acepta `extra_imperative` y `extra_informative` (objetos JSON opcionales):

| Valor recibido | Efecto en BD |
|---|---|
| `{"k":"v"}` | Sobreescribe el campo |
| `null` (omitido) | COALESCE: conserva el valor existente |
| `{}` | Borra el campo (NULL en BD) |

Solo `extra_imperative` dispara `bump_version`. `extra_informative` es puramente informativo.

## Ciclo de vida de clientes

```
active   → registrándose normalmente (last_seen reciente)
inactive → sin heartbeat desde TTL_INACTIVE_DAYS días  → sube versión
archived → sin heartbeat desde TTL_ARCHIVE_DAYS días   → solo histórico
(purge)  → eliminación definitiva tras TTL_PURGE_DAYS  → TTL_PURGE_DAYS=0: nunca
```

Las transiciones se ejecutan en cada arranque de VAS y pueden forzarse con `vas-cleanup`.

Un cliente `inactive` o `archived` vuelve a `active` automáticamente al hacer `POST /register` o `POST /heartbeat`. Ambos endpoints suben versión al reactivar, para que los consumidores detecten el cambio.

## Flujo de arranque

```
load_config()         → /etc/vas/vas.conf + /etc/vas/vas.conf.d/*.conf
validate_paths()      → crea /var/lib/vas si falta; FATAL si sin permisos
database.init_db()    → CREATE TABLE IF NOT EXISTS clients (+ migraciones)
lifespan (startup):
  run_lifecycle()
    → active  → inactive  (TTL_INACTIVE_DAYS; bump_version si hay cambios)
    → inactive → archived (TTL_ARCHIVE_DAYS)
    → archived → DELETE   (TTL_PURGE_DAYS; 0 = desactivado)
[endpoints activos]
```

## Limpieza manual (`vas-cleanup`)

Herramienta interactiva con interfaz Zenity, dialog o terminal. Permite ejecutar cada paso del ciclo de vida de forma independiente o como ciclo completo, con confirmación antes de cada operación destructiva.

## Configuración

Fichero principal: `/etc/vas/vas.conf`  
Overlays (orden lexical): `/etc/vas/vas.conf.d/*.conf`

| Variable | Defecto | Descripción |
|---|---|---|
| `PORT` | `8000` | Puerto HTTP de escucha |
| `DB_PATH` | `/var/lib/vas/vas.db` | Base de datos SQLite |
| `VERSION_FILE` | `/var/lib/vas/version` | Fichero de versión |
| `TTL_INACTIVE_DAYS` | `30` | Días sin heartbeat para pasar a `inactive` |
| `TTL_ARCHIVE_DAYS` | `90` | Días sin heartbeat para pasar a `archived` |
| `TTL_PURGE_DAYS` | `365` | Días en `archived` antes de eliminar (0 = nunca) |
| `LOG_LEVEL` | `normal` | Nivel de log: `no` (silencio), `normal` (eventos importantes), `debug` (detallado) |
| `LOG_FILE` | — | Fichero de log adicional con timestamp ISO-8601 UTC (vacío = solo journald) |
| `HOOKS_DIR` | `/etc/vas/hooks.d` | Directorio de scripts ejecutados tras cada `bump_version` (fire and forget) |

> Los TTLs deben ser notablemente mayores que `CHECK_SECONDS` de los clientes VAC. Con `CHECK_SECONDS=300` y `TTL_INACTIVE_DAYS=30`, el margen es de más de 8000× .

## Notificación push: hooks de bump_version

Tras cada `bump_version()`, VAS lanza en paralelo (fire and forget) todos los scripts ejecutables de `HOOKS_DIR`. No espera resultados ni los registra en el flujo principal.

Cada hook recibe:

| Variable | Valor |
|---|---|
| `VAS_HOST` | URL base de esta instancia (`http://127.0.0.1:PORT`) |
| `VAS_VERSION` | Versión que disparó el evento |

### Hook vcd-local

El hook de ejemplo `vcd-local` implementa la notificación push a instancias VCD-Aware:

1. Consulta `GET /clients?extra_key=inform` — clientes que tengan la clave `inform` en sus extras.
2. Para cada uno, envía un datagrama UDP a `extra_imperative.inform.url`.
3. VCD-Aware recibe el UDP, interrumpe su `sleep` y consulta `/version` inmediatamente.

```bash
# Activar el hook
cp /usr/share/vas/hooks.d.examples/vcd-local /etc/vas/hooks.d/
chmod +x /etc/vas/hooks.d/vcd-local
```

El hook requiere que cada equipo cliente publique en VAC:
```bash
echo '{"url":"10.0.1.5:9876"}' | vac-register --imperative --key inform -
```

Y que VCD tenga `BUMP_LISTEN_PORT=9876` configurado. Ver documentación de VCD-Aware.

## Seguridad

- El servicio corre como usuario dedicado `vas` (sin shell, sin home).
- El parser de configuración no ejecuta código: usa `split("=", 1)` + strip de comillas.
- `GET /clients` no incluye el UUID en el listado público; solo `GET /clients/{id}` lo devuelve (quien lo consulta ya lo conoce).

## Logging

Formato de salida: `[VAS] [SCOPE] mensaje` (normal) · `[VAS] [DEBUG] [SCOPE] mensaje` (debug).

```bash
journalctl -u vas -f                        # tiempo real
journalctl -u vas | grep '\[DEBUG\]'        # solo debug
journalctl -u vas | grep '\[ERROR\]'        # solo errores
journalctl -u vas | grep '\[LIFECYCLE\]'    # transiciones de ciclo de vida
```

El prefijo `[VAS]` lo añade `vas_log.py` automáticamente. Ver la wiki ([Logging](../../vx-dga-l-vas.wiki/-/blob/main/Logging.md)) para la referencia completa de scopes.

## Servicio systemd

```bash
systemctl status vas
systemctl restart vas
journalctl -u vas -f
```

## Construcción del paquete

```bash
dpkg-buildpackage -us -uc -b
```
