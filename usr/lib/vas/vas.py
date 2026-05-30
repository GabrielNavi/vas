#!/usr/bin/env python3
"""
vas.py — Versatile Autoregistration Server (VAS).

Servidor FastAPI que mantiene el inventario de red de equipos.
Expone una API REST consumible por cualquier servicio (VAC, veyon-sync, etc.).

Endpoints:
  POST /register            → registra o actualiza un cliente; retorna versión actual
  POST /heartbeat           → actualiza last_seen sin tocar datos ni versión
  GET  /health              → {"status": "ok"} sin side-effects (proxies y monitorización)
  GET  /version             → versión del registro (YYYYMMDDHHMMSS)
  GET  /clients             → clientes activos (default) o filtrados por ?status= y ?extra_key=
  GET  /clients/{id}        → cliente individual por UUID

Ciclo de vida de clientes (gestionado en startup y periódicamente vía LIFECYCLE_INTERVAL):
  active   → registrándose normalmente
  inactive → sin heartbeat desde TTL_INACTIVE (sube versión)
  archived → sin heartbeat desde TTL_ARCHIVE (histórico)
  (purge)  → eliminación definitiva tras TTL_PURGE (0 = nunca)
"""
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator

import database
from vas_log import log, log_debug, setup_logging

CONFIG_FILE = "/etc/vas/vas.conf"
CONFIG_DIR  = "/etc/vas/vas.conf.d"


# ---------------------------------------------------------------------------
# Parser de duraciones
# ---------------------------------------------------------------------------

def parse_duration(s: str) -> int:
    """
    Convierte una cadena de duración a segundos.

    Formatos aceptados: 30d, 12h, 90m, 60s (insensible a mayúsculas).
    Sin sufijo: emite [WARN] y asume días para compatibilidad.
    Ejemplos: '30d' → 2592000, '12h' → 43200, '0d' → 0 (purga desactivada).
    """
    s = str(s).strip()
    if not s:
        raise ValueError("Duración vacía")
    suffix = s[-1].lower()
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    if suffix in multipliers:
        return int(s[:-1]) * multipliers[suffix]
    # Sin sufijo: asumir días con advertencia
    log(f"[WARN] Duración sin unidad: '{s}'. Asumiendo días. Usa sufijo: s, m, h, d.")
    return int(s) * 86400


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """
    Carga la configuración desde CONFIG_FILE y los overlays en CONFIG_DIR.

    El parser no ejecuta código: divide cada línea en clave=valor y elimina
    comillas. Las claves desconocidas se ignoran silenciosamente.
    Los overlays en CONFIG_DIR se aplican en orden lexical y sobreescriben
    el fichero principal.

    Devuelve un diccionario con los valores efectivos.
    """
    cfg = {
        "PORT":               "8000",
        "DB_PATH":            "/var/lib/vas/vas.db",
        "VERSION_FILE":       "/var/lib/vas/version",
        "TTL_INACTIVE":       "30d",
        "TTL_ARCHIVE":        "90d",
        "TTL_PURGE":          "365d",
        "LIFECYCLE_INTERVAL": "24h",
        "LOG_LEVEL":          "normal",
        "LOG_FILE":           "",
        "HOOKS_DIR":          "/etc/vas/hooks.d",
        "HOOKS_LOG":          "",
    }

    def _apply_file(path: str) -> None:
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")

    _apply_file(CONFIG_FILE)

    if os.path.isdir(CONFIG_DIR):
        for name in sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith(".conf")):
            _apply_file(os.path.join(CONFIG_DIR, name))

    return cfg


# Cargar configuración e inicializar logging antes de cualquier otra operación.
config = load_config()
setup_logging(config["LOG_LEVEL"], config["LOG_FILE"])

log_debug(
    f"[CONFIG] Configuración efectiva: "
    f"PORT={config['PORT']} DB={config['DB_PATH']} "
    f"TTL_INACTIVE={config['TTL_INACTIVE']} "
    f"TTL_ARCHIVE={config['TTL_ARCHIVE']} "
    f"TTL_PURGE={config['TTL_PURGE']} "
    f"LIFECYCLE_INTERVAL={config['LIFECYCLE_INTERVAL']} "
    f"LOG_LEVEL={config['LOG_LEVEL']}"
)

# Inyectar rutas en el módulo database
database.DB_PATH      = config["DB_PATH"]
database.VERSION_FILE = config["VERSION_FILE"]
database.HOOKS_DIR    = config["HOOKS_DIR"]
database.HOOKS_LOG    = config["HOOKS_LOG"] or None
database.VAS_BASE_URL = f"http://127.0.0.1:{config['PORT']}"


# ---------------------------------------------------------------------------
# Validación de rutas
# ---------------------------------------------------------------------------

def validate_paths() -> None:
    """
    Verifica que los directorios de DB_PATH y VERSION_FILE existen y tienen
    permisos de escritura. Los crea si faltan.

    Lanza RuntimeError si alguna ruta no es accesible (fallo rápido intencional).
    """
    errors = []

    for path, var_name in [
        (database.DB_PATH,      "DB_PATH"),
        (database.VERSION_FILE, "VERSION_FILE"),
    ]:
        dir_path = os.path.dirname(path) or "."

        if not os.path.exists(dir_path):
            try:
                os.makedirs(dir_path, exist_ok=True)
                log(f"[PATHS] Directorio creado: {dir_path}")
            except OSError as e:
                errors.append(f"{var_name}: no se puede crear {dir_path}: {e}")
                continue

        if not os.access(dir_path, os.W_OK):
            errors.append(f"{var_name}: sin permisos de escritura en {dir_path}")
            continue

        if os.path.exists(path) and not os.access(path, os.W_OK):
            errors.append(f"{var_name}: sin permisos de escritura en {path}")
            continue

        log_debug(f"[PATHS] OK: {path}")

    if errors:
        for err in errors:
            log(f"[ERROR] {err}")
        raise RuntimeError(f"Configuración inválida: {len(errors)} problema(s) de permisos")


# Ejecutar validación a nivel de módulo: fallo fatal antes de arrancar uvicorn
try:
    validate_paths()
except RuntimeError as e:
    log(f"[ERROR] FATAL: {e}")
    raise

# Inicializar base de datos (CREATE TABLE IF NOT EXISTS + migración status + fichero de versión)
database.init_db()


# ---------------------------------------------------------------------------
# Gestión del ciclo de vida de clientes
# ---------------------------------------------------------------------------

def run_lifecycle() -> None:
    """
    Ejecuta las tres transiciones del ciclo de vida de clientes.

    Llamado en startup y periódicamente cada LIFECYCLE_INTERVAL.
    1. active → inactive: sin heartbeat desde TTL_INACTIVE. Sube versión.
    2. inactive → archived: sin heartbeat desde TTL_ARCHIVE. No sube versión.
    3. archived → DELETE: tras TTL_PURGE. TTL_PURGE=0 desactiva el borrado.
    """
    ttl_inactive = parse_duration(config.get("TTL_INACTIVE", "30d"))
    ttl_archive  = parse_duration(config.get("TTL_ARCHIVE",  "90d"))
    ttl_purge    = parse_duration(config.get("TTL_PURGE",   "365d"))

    log_debug(
        f"[LIFECYCLE] TTL "
        f"inactive={config.get('TTL_INACTIVE','30d')} "
        f"archive={config.get('TTL_ARCHIVE','90d')} "
        f"purge={config.get('TTL_PURGE','365d')}"
    )

    marked = database.mark_inactive_clients(ttl_inactive)
    if marked > 0:
        version = database.bump_version()
        log(f"[LIFECYCLE] {marked} cliente(s) → inactive. Versión publicada: {version}")
    else:
        log_debug("[LIFECYCLE] Sin clientes nuevos a inactivar.")

    archived = database.archive_clients(ttl_archive)
    if archived > 0:
        log(f"[LIFECYCLE] {archived} cliente(s) → archived.")

    purged = database.purge_clients(ttl_purge)
    if purged > 0:
        log(f"[LIFECYCLE] {purged} cliente(s) eliminado(s) definitivamente.")


# ---------------------------------------------------------------------------
# Ciclo de vida de la aplicación (startup / shutdown)
# ---------------------------------------------------------------------------

async def _lifecycle_loop(interval: int) -> None:
    """Ejecuta run_lifecycle() cada `interval` segundos en background."""
    while True:
        await asyncio.sleep(interval)
        try:
            run_lifecycle()
        except Exception as e:
            log(f"[ERROR] Fallo en lifecycle periódico (no fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Contexto de vida de la aplicación FastAPI.

    En startup ejecuta run_lifecycle() y arranca la tarea periódica de lifecycle.
    Los errores no bloquean el arranque (la limpieza es mantenimiento no crítico).
    """
    log(
        f"[STARTUP] VAS arrancando en puerto {config.get('PORT', '8000')} | "
        f"DB: {config['DB_PATH']}"
    )
    try:
        run_lifecycle()
        log("[STARTUP] Listo para recibir peticiones.")
    except Exception as e:
        log(f"[ERROR] Fallo en startup (no fatal): {e}")

    interval = parse_duration(config.get("LIFECYCLE_INTERVAL", "24h"))
    log_debug(f"[LIFECYCLE] Tarea periódica cada {config.get('LIFECYCLE_INTERVAL','24h')}.")
    task = asyncio.create_task(_lifecycle_loop(interval))

    yield  # La aplicación sirve peticiones a partir de aquí

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log("[SHUTDOWN] Cerrando VAS.")


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------

class Client(BaseModel):
    """
    Representación de un cliente VAC en el cuerpo de POST /register.

    extra_imperative  → dict arbitrario; los cambios disparan bump_version.
    extra_informative → dict arbitrario; nunca dispara versión (solo informativo).
    Ambos son opcionales y null-seguros.
    """
    id:                str
    hostname:          str
    ip:                str
    mac:               Optional[str]  = None
    extra_imperative:  Optional[dict] = None
    extra_informative: Optional[dict] = None

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("id no puede estar vacío")
        return v.strip()

    @field_validator("ip")
    @classmethod
    def ip_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("ip no puede estar vacía")
        return v.strip()


class HeartbeatRequest(BaseModel):
    """Cuerpo de POST /heartbeat: solo el UUID del cliente."""
    id: str

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("id no puede estar vacío")
        return v.strip()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Healthcheck para proxies inversos y monitorización. Sin side-effects ni log."""
    return {"status": "ok"}


@app.post("/register")
def register(client: Client):
    """
    Registra o actualiza un cliente en el inventario.

    Siempre actualiza last_seen y restaura status a 'active' (reactiva
    clientes inactivos o archivados automáticamente).
    Sube versión solo si datos o status han cambiado.
    Retorna {status, version}.
    """
    try:
        mac = client.mac or ""

        changed = database.client_has_changed(
            client.id, client.hostname, client.ip, mac,
            extra_imperative=client.extra_imperative,
        )
        database.add_or_update_client(
            client.id, client.hostname, client.ip, mac,
            extra_imperative=client.extra_imperative,
            extra_informative=client.extra_informative,
        )

        if changed:
            version = database.bump_version()
            log(
                f"[REGISTER] NUEVO/CAMBIO → {client.id} "
                f"host={client.hostname} ip={client.ip} mac={mac or '(vacía)'} "
                f"versión={version}"
            )
        else:
            version = database.get_version()
            log_debug(
                f"[REGISTER] HEARTBEAT → {client.id} ({client.hostname}) "
                f"sin cambios. last_seen actualizado. versión={version}"
            )

        return {"status": "ok", "version": version}

    except Exception as e:
        log(f"[ERROR] Fallo en POST /register [{client.id}]: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/heartbeat")
def heartbeat(hb: HeartbeatRequest):
    """
    Heartbeat ligero: actualiza last_seen y restaura status='active'.

    No toca datos del cliente (hostname, ip, mac, extras).
    Si el cliente estaba inactive o archived, sube versión para que los
    consumidores (VAC, veyon-sync) detecten la reactivación en el inventario.
    Retorna 404 si el UUID es desconocido — señal para que VAC se re-registre.
    Retorna {status, version}.
    """
    try:
        database.touch_client(hb.id)
        version = database.get_version()
        log_debug(f"[HEARTBEAT] OK → {hb.id} versión={version}")
        return {"status": "ok", "version": version}
    except ValueError:
        log(f"[HEARTBEAT] No encontrado: {hb.id}")
        raise HTTPException(status_code=404, detail="Client not found")
    except Exception as e:
        log(f"[ERROR] Fallo en POST /heartbeat [{hb.id}]: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/version")
def version():
    """
    Devuelve la versión actual del registro.

    Los clientes (VAC, veyon-sync) comparan esta versión con la suya local
    para decidir si deben descargar el inventario actualizado.
    Formato YYYYMMDDHHMMSS (timestamp UTC del último cambio).
    """
    ver = database.get_version()
    log_debug(f"[VERSION] Consulta → {ver}")
    return {"version": ver}


@app.get("/clients")
def list_clients(
    status: str = Query(
        default="active",
        description="Filtro de estado: active (default) | inactive | archived | all",
    ),
    extra_key: str = Query(
        default=None,
        description="Filtro por clave de extra: retorna solo clientes que tengan "
                    "esa clave en extra_imperative o extra_informative.",
    ),
):
    """
    Devuelve clientes filtrados por estado y, opcionalmente, por clave de extra.

    ?status=active              → solo activos (default)
    ?status=inactive            → solo inactivos
    ?status=archived            → solo archivados
    ?status=all                 → todos los estados
    ?extra_key=cups             → solo clientes con la clave 'cups' en algún campo extra
    ?extra_key=hardware&status=all → combinable con status

    La respuesta incluye el valor completo de extra_imperative y extra_informative;
    el consumidor decide qué hacer con los campos extra devueltos.
    """
    valid = {"active", "inactive", "archived", "all"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"status inválido. Valores: {sorted(valid)}")

    try:
        clients = database.get_all_clients(status=status, extra_key=extra_key)
        log_debug(
            f"[CLIENTS] Listado servido [status={status}"
            + (f" extra_key={extra_key}" if extra_key else "")
            + f"]: {len(clients)} cliente(s)"
        )
        return {"clients": clients}
    except Exception as e:
        log(f"[ERROR] Fallo en GET /clients: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    """
    Devuelve los datos de un cliente específico por UUID.

    Retorna 404 si el UUID no existe en el registro.
    Incluye el campo status para diagnóstico del ciclo de vida.
    """
    log_debug(f"[CLIENTS] Consulta individual: {client_id}")
    client = database.get_client(client_id)

    if client is None:
        log(f"[CLIENTS] No encontrado: {client_id}")
        raise HTTPException(status_code=404, detail="Client not found")

    log_debug(
        f"[CLIENTS] Encontrado: {client_id} → "
        f"{client['hostname']} / {client['ip']} [{client['status']}]"
    )
    return client
