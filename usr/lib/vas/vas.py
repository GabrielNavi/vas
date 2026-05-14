#!/usr/bin/env python3
"""
vas.py — Vitalinux Autoregistration Server (VAS).

Servidor FastAPI que mantiene el inventario de red de equipos Vitalinux.
Expone una API REST consumible por cualquier servicio (VAC, veyon-sync, etc.).

Endpoints:
  POST /register            → registra o actualiza un cliente; retorna versión actual
  POST /heartbeat           → actualiza last_seen sin tocar datos ni versión
  GET  /version             → versión del registro (YYYYMMDDHHMMSS)
  GET  /clients             → clientes activos (default) o filtrados por ?status=
  GET  /clients/{id}        → cliente individual por UUID

Ciclo de vida de clientes (gestionado en startup y por vas-cleanup):
  active   → registrándose normalmente
  inactive → sin heartbeat desde TTL_INACTIVE_DAYS días (sube versión)
  archived → sin heartbeat desde TTL_ARCHIVE_DAYS días (histórico)
  (purge)  → eliminación definitiva tras TTL_PURGE_DAYS (0 = nunca)
"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

import database
from vas_log import log, log_debug, setup_logging

CONFIG_FILE = "/etc/vas/vas.conf"
CONFIG_DIR  = "/etc/vas/vas.conf.d"


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
        "TTL_INACTIVE_DAYS":  "30",
        "TTL_ARCHIVE_DAYS":   "90",
        "TTL_PURGE_DAYS":     "365",
        "LOG_LEVEL":          "normal",
        "LOG_FILE":           "",
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
    f"[VAS-CONFIG] Configuración efectiva: "
    f"PORT={config['PORT']} DB={config['DB_PATH']} "
    f"TTL_INACTIVE={config['TTL_INACTIVE_DAYS']}d "
    f"TTL_ARCHIVE={config['TTL_ARCHIVE_DAYS']}d "
    f"TTL_PURGE={config['TTL_PURGE_DAYS']}d "
    f"LOG_LEVEL={config['LOG_LEVEL']}"
)

# Inyectar rutas en el módulo database
database.DB_PATH      = config["DB_PATH"]
database.VERSION_FILE = config["VERSION_FILE"]


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
                log(f"[VAS-PATHS] Directorio creado: {dir_path}")
            except OSError as e:
                errors.append(f"{var_name}: no se puede crear {dir_path}: {e}")
                continue

        if not os.access(dir_path, os.W_OK):
            errors.append(f"{var_name}: sin permisos de escritura en {dir_path}")
            continue

        if os.path.exists(path) and not os.access(path, os.W_OK):
            errors.append(f"{var_name}: sin permisos de escritura en {path}")
            continue

        log_debug(f"[VAS-PATHS] OK: {path}")

    if errors:
        for err in errors:
            log(f"[VAS-ERROR] {err}")
        raise RuntimeError(f"Configuración inválida: {len(errors)} problema(s) de permisos")


# Ejecutar validación a nivel de módulo: fallo fatal antes de arrancar uvicorn
try:
    validate_paths()
except RuntimeError as e:
    log(f"[VAS-ERROR] FATAL: {e}")
    raise

# Inicializar base de datos (CREATE TABLE IF NOT EXISTS + migración status + fichero de versión)
database.init_db()


# ---------------------------------------------------------------------------
# Gestión del ciclo de vida de clientes
# ---------------------------------------------------------------------------

def run_lifecycle() -> None:
    """
    Ejecuta las tres transiciones del ciclo de vida de clientes al arrancar VAS.

    1. active → inactive: clientes sin heartbeat desde TTL_INACTIVE_DAYS días.
       Sube versión si hay cambios (los consumidores dejan de ver al equipo).
    2. inactive → archived: clientes inactivos desde TTL_ARCHIVE_DAYS días.
       No sube versión (ya estaban fuera del inventario activo).
    3. archived → DELETE: eliminación definitiva tras TTL_PURGE_DAYS días.
       TTL_PURGE_DAYS=0 desactiva el borrado (histórico permanente).
    """
    ttl_inactive = int(config.get("TTL_INACTIVE_DAYS", 30))
    ttl_archive  = int(config.get("TTL_ARCHIVE_DAYS",  90))
    ttl_purge    = int(config.get("TTL_PURGE_DAYS",   365))

    log_debug(
        f"[VAS-LIFECYCLE] TTL inactive={ttl_inactive}d archive={ttl_archive}d purge={ttl_purge}d"
    )

    marked = database.mark_inactive_clients(ttl_inactive)
    if marked > 0:
        version = database.bump_version()
        log(f"[VAS-LIFECYCLE] {marked} cliente(s) → inactive. Versión publicada: {version}")
    else:
        log_debug("[VAS-LIFECYCLE] Sin clientes nuevos a inactivar.")

    archived = database.archive_clients(ttl_archive)
    if archived > 0:
        log(f"[VAS-LIFECYCLE] {archived} cliente(s) → archived.")

    purged = database.purge_clients(ttl_purge)
    if purged > 0:
        log(f"[VAS-LIFECYCLE] {purged} cliente(s) eliminado(s) definitivamente.")


# ---------------------------------------------------------------------------
# Ciclo de vida de la aplicación (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Contexto de vida de la aplicación FastAPI.

    En startup ejecuta run_lifecycle() para gestionar los estados de los clientes.
    Los errores no bloquean el arranque (la limpieza es mantenimiento no crítico).
    """
    log(
        f"[VAS-STARTUP] VAS arrancando en puerto {config.get('PORT', '8000')} | "
        f"DB: {config['DB_PATH']}"
    )
    try:
        run_lifecycle()
        log("[VAS-STARTUP] Listo para recibir peticiones.")
    except Exception as e:
        log(f"[VAS-ERROR] Fallo en startup (no fatal): {e}")

    yield  # La aplicación sirve peticiones a partir de aquí

    log("[VAS-SHUTDOWN] Cerrando VAS.")


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


class HeartbeatRequest(BaseModel):
    """Cuerpo de POST /heartbeat: solo el UUID del cliente."""
    id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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
                f"[VAS-REGISTER] NUEVO/CAMBIO → {client.id} "
                f"host={client.hostname} ip={client.ip} mac={mac or '(vacía)'} "
                f"versión={version}"
            )
        else:
            version = database.get_version()
            log_debug(
                f"[VAS-REGISTER] HEARTBEAT → {client.id} ({client.hostname}) "
                f"sin cambios. last_seen actualizado. versión={version}"
            )

        return {"status": "ok", "version": version}

    except Exception as e:
        log(f"[VAS-ERROR] Fallo en POST /register [{client.id}]: {e}")
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
        log_debug(f"[VAS-HEARTBEAT] OK → {hb.id} versión={version}")
        return {"status": "ok", "version": version}
    except ValueError:
        log(f"[VAS-HEARTBEAT] No encontrado: {hb.id}")
        raise HTTPException(status_code=404, detail="Client not found")
    except Exception as e:
        log(f"[VAS-ERROR] Fallo en POST /heartbeat [{hb.id}]: {e}")
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
    log_debug(f"[VAS-VERSION] Consulta → {ver}")
    return {"version": ver}


@app.get("/clients")
def list_clients(
    status: str = Query(
        default="active",
        description="Filtro de estado: active (default) | inactive | archived | all",
    )
):
    """
    Devuelve clientes filtrados por estado.

    ?status=active   → solo activos (default; consumidores VAC/veyon-sync)
    ?status=inactive → solo inactivos (sin heartbeat reciente)
    ?status=archived → solo archivados (histórico)
    ?status=all      → todos los estados

    Cada entrada incluye: id, hostname, ip, mac, status, last_seen.
    """
    valid = {"active", "inactive", "archived", "all"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"status inválido. Valores: {sorted(valid)}")

    try:
        clients = database.get_all_clients(status=status)
        log_debug(f"[VAS-CLIENTS] Listado servido [{status}]: {len(clients)} cliente(s)")
        return {"clients": clients}
    except Exception as e:
        log(f"[VAS-ERROR] Fallo en GET /clients: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    """
    Devuelve los datos de un cliente específico por UUID.

    Retorna 404 si el UUID no existe en el registro.
    Incluye el campo status para diagnóstico del ciclo de vida.
    """
    log_debug(f"[VAS-CLIENTS] Consulta individual: {client_id}")
    client = database.get_client(client_id)

    if client is None:
        log(f"[VAS-CLIENTS] No encontrado: {client_id}")
        raise HTTPException(status_code=404, detail="Client not found")

    log_debug(
        f"[VAS-CLIENTS] Encontrado: {client_id} → "
        f"{client['hostname']} / {client['ip']} [{client['status']}]"
    )
    return client
