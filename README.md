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
- Versión: 0.6-2
- Arquitectura: all
- Mantenedor: Gabriel Navia \<correos@gabrielnav.es\>
- Licencia: GPL-3.0+

## Archivos instalados

| Ruta | Descripción |
|---|---|
| `usr/lib/vas/vas.py` | Aplicación FastAPI principal |
| `usr/lib/vas/database.py` | Capa de persistencia SQLite |
| `usr/bin/vas` | Wrapper de arranque (extrae PORT, lanza uvicorn) |
| `usr/bin/vas-cleanup` | Herramienta interactiva de limpieza manual |
| `etc/vas/vas.conf` | Configuración editable |
| `lib/systemd/system/vas.service` | Unidad systemd |

## API

| Método | Endpoint | Descripción |
|---|---|---|
| `POST` | `/register` | Registra o actualiza un cliente. Retorna `{status, version}`. |
| `GET` | `/version` | Versión actual del registro (`YYYYMMDDHHMMSS`). |
| `GET` | `/clients` | Lista completa: `{clients: [{id, hostname, ip, mac, last_seen}]}` |
| `GET` | `/clients/{id}` | Cliente individual por UUID. 404 si no existe. |

La versión solo se incrementa cuando cambian datos reales de algún cliente. Los heartbeats periódicos de VAC actualizan `last_seen` sin modificar la versión.

## Flujo de arranque

```
load_config()         → /etc/vas/vas.conf + /etc/vas/vas.conf.d/*.conf
validate_paths()      → crea /var/lib/vas si falta; FATAL si sin permisos
database.init_db()    → CREATE TABLE IF NOT EXISTS clients
lifespan (startup):
  cleanup_old_clients(CLIENT_TTL_DAYS)
    → DELETE WHERE last_seen < (ahora - TTL)
    → si eliminados > 0: bump_version()
[endpoints activos]
```

## Limpieza automática (TTL)

VAS elimina clientes inactivos en cada arranque según `CLIENT_TTL_DAYS`. Un cliente activo que ejecute VAC cada `CHECK_SECONDS` nunca será purgado (relación de seguridad: `CHECK_SECONDS << CLIENT_TTL_DAYS × 86400`).

## Limpieza manual (`vas-cleanup`)

Herramienta interactiva con interfaz Zenity, dialog o terminal. Pide confirmación, elimina clientes más antiguos que N días y actualiza la versión. `vx-dga-l-veyon-sync` detectará el cambio en su siguiente ciclo.

## Configuración

Fichero principal: `/etc/vas/vas.conf`  
Overlays (orden lexical): `/etc/vas/vas.conf.d/*.conf`

| Variable | Defecto | Descripción |
|---|---|---|
| `PORT` | `8000` | Puerto HTTP de escucha |
| `DB_PATH` | `/var/lib/vas/vas.db` | Base de datos SQLite |
| `VERSION_FILE` | `/var/lib/vas/version` | Fichero de versión |
| `CLIENT_TTL_DAYS` | `30` | Días de inactividad antes de purgar un cliente |

> `CLIENT_TTL_DAYS` debe ser notablemente mayor que `CHECK_SECONDS` de los clientes VAC. Con `CHECK_SECONDS=300`, cualquier valor superior a 1 día es seguro.

## Seguridad

- El servicio corre como usuario dedicado `_vas` (sin shell, sin home).
- El parser de configuración no ejecuta código: usa `split("=", 1)` + strip de comillas.

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
