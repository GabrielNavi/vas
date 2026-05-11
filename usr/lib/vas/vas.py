#!/usr/bin/env python3
"""
vas.py — Vitalinux Autoregistration Server (VAS).

Servidor FastAPI que mantiene el inventario de red de equipos Vitalinux.
Expone una API REST consumible por cualquier servicio (VAC, veyon-sync, etc.).

Endpoints:
  POST /register       → registra o actualiza un cliente; retorna versión actual
  GET  /version        → versión del registro (YYYYMMDDHHMMSS)
  GET  /clients        → listado completo con last_seen
  GET  /clients/{id}   → cliente individual por UUID
"""
import os
import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import database

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
        "PORT":            "8000",
        "DB_PATH":         "/var/lib/vas/vas.db",
        "VERSION_FILE":    "/var/lib/vas/version",
        "CLIENT_TTL_DAYS": "30",
    }

    def _apply_file(path: str) -> None:
        """Lee un fichero de configuración y aplica sus valores sobre cfg."""
        if not os.path.isfile(path):
            return
        loaded = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                cfg[k] = v
                loaded.append(k)
        print(f"[VAS-CONFIG] {path}: cargadas {len(loaded)} clave(s): {', '.join(loaded)}", flush=True)

    _apply_file(CONFIG_FILE)

    if os.path.isdir(CONFIG_DIR):
        overlays = sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith(".conf"))
        if overlays:
            print(f"[VAS-CONFIG] Overlays encontrados en {CONFIG_DIR}: {overlays}", flush=True)
        for name in overlays:
            _apply_file(os.path.join(CONFIG_DIR, name))
    else:
        print(f"[VAS-CONFIG] Sin directorio de overlays ({CONFIG_DIR})", flush=True)

    return cfg


# Cargar configuración e inyectar rutas en el módulo database
config = load_config()

database.DB_PATH      = config["DB_PATH"]
database.VERSION_FILE = config["VERSION_FILE"]

print(
    f"[VAS-CONFIG] Configuración efectiva: "
    f"PORT={config['PORT']} DB={config['DB_PATH']} TTL={config['CLIENT_TTL_DAYS']}d",
    flush=True,
)


# ---------------------------------------------------------------------------
# Validación de rutas
# ---------------------------------------------------------------------------

def validate_paths() -> None:
    """
    Verifica que los directorios de DB_PATH y VERSION_FILE existen y tienen
    permisos de escritura. Los crea si faltan.

    Lanza RuntimeError si alguna ruta no es accesible, lo que provoca que
    uvicorn no arranque (fallo rápido intencional).
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
                print(f"[VAS-PATHS] Directorio creado: {dir_path}", flush=True)
            except OSError as e:
                errors.append(f"{var_name}: no se puede crear {dir_path}: {e}")
                continue

        if not os.access(dir_path, os.W_OK):
            errors.append(f"{var_name}: sin permisos de escritura en {dir_path}")
            continue

        if os.path.exists(path) and not os.access(path, os.W_OK):
            errors.append(f"{var_name}: sin permisos de escritura en {path}")
            continue

        print(f"[VAS-PATHS] OK: {path}", flush=True)

    if errors:
        for err in errors:
            print(f"[VAS-ERROR] {err}", flush=True)
        raise RuntimeError(f"Configuración inválida: {len(errors)} problema(s) de permisos")


# Ejecutar validación a nivel de módulo: fallo fatal antes de arrancar uvicorn
try:
    validate_paths()
except RuntimeError as e:
    print(f"[VAS-ERROR] FATAL: {e}", flush=True)
    raise

# Inicializar base de datos (CREATE TABLE IF NOT EXISTS + fichero de versión)
database.init_db()


# ---------------------------------------------------------------------------
# Limpieza de clientes inactivos
# ---------------------------------------------------------------------------

def cleanup_old_clients(days: int) -> int:
    """
    Elimina clientes cuyo last_seen sea anterior a (ahora - days días).

    Usa una única transacción SQLite para COUNT + DELETE, evitando
    inconsistencias si el proceso se interrumpe entre ambas operaciones.

    Devuelve el número de clientes eliminados.
    """
    cutoff     = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"[VAS-CLEANUP] TTL: {days} día(s). Cutoff: {cutoff_str}", flush=True)

    conn = database.get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM clients")
    total_before = cur.fetchone()[0]
    print(f"[VAS-CLEANUP] Clientes en BD antes de limpieza: {total_before}", flush=True)

    cur.execute("DELETE FROM clients WHERE last_seen < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    total_after = total_before - deleted
    if deleted > 0:
        print(
            f"[VAS-CLEANUP] {deleted} cliente(s) purgado(s) por inactividad. "
            f"Restantes: {total_after}",
            flush=True,
        )
    else:
        print(f"[VAS-CLEANUP] Sin clientes inactivos. Total: {total_after}", flush=True)

    return deleted


# ---------------------------------------------------------------------------
# Ciclo de vida de la aplicación (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Contexto de vida de la aplicación FastAPI.

    En startup:
      1. Muestra la configuración efectiva.
      2. Ejecuta la limpieza de clientes inactivos según CLIENT_TTL_DAYS.
      3. Si se purgaron clientes, publica una nueva versión para que los
         consumidores (veyon-sync) detecten el cambio.

    Los errores de startup son capturados y logueados sin bloquear el arranque,
    ya que la limpieza es una operación de mantenimiento no crítica.
    """
    print(
        f"[VAS-STARTUP] VAS arrancando en puerto {config.get('PORT', '8000')} | "
        f"DB: {config['DB_PATH']} | TTL: {config['CLIENT_TTL_DAYS']} días",
        flush=True,
    )
    try:
        ttl_days = int(config.get("CLIENT_TTL_DAYS", 30))
        deleted  = cleanup_old_clients(ttl_days)

        if deleted > 0:
            version = database.bump_version()
            print(
                f"[VAS-STARTUP] Versión publicada tras limpieza: {version}",
                flush=True,
            )

        print("[VAS-STARTUP] Listo para recibir peticiones.", flush=True)

    except Exception as e:
        print(f"[VAS-ERROR] Fallo en startup (no fatal): {e}", flush=True)

    yield  # La aplicación sirve peticiones a partir de aquí

    print("[VAS-SHUTDOWN] Cerrando VAS.", flush=True)


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------

class Client(BaseModel):
    """Representación de un cliente VAC en el cuerpo de POST /register."""
    id:       str
    hostname: str
    ip:       str
    mac:      Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/register")
def register(client: Client):
    """
    Registra o actualiza un cliente en el inventario.

    Comportamiento:
    - Siempre actualiza last_seen (mantiene el cliente vivo frente al TTL).
    - Sube la versión SOLO si hostname, ip o mac han cambiado.
    - Retorna {status, version} donde version es la versión actual del registro.

    Esto permite a VAC usar este endpoint como heartbeat periódico sin
    generar versiones innecesarias cuando los datos no cambian.
    """
    try:
        mac = client.mac or ""

        changed = database.client_has_changed(client.id, client.hostname, client.ip, mac)
        database.add_or_update_client(client.id, client.hostname, client.ip, mac)

        if changed:
            version = database.bump_version()
            print(
                f"[VAS-REGISTER] NUEVO/CAMBIO → {client.id} "
                f"host={client.hostname} ip={client.ip} mac={mac or '(vacía)'} "
                f"versión={version}",
                flush=True,
            )
        else:
            version = database.get_version()
            print(
                f"[VAS-REGISTER] HEARTBEAT → {client.id} ({client.hostname}) "
                f"sin cambios. last_seen actualizado. versión={version}",
                flush=True,
            )

        return {"status": "ok", "version": version}

    except Exception as e:
        print(f"[VAS-ERROR] Fallo en POST /register [{client.id}]: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/version")
def version():
    """
    Devuelve la versión actual del registro.

    Los clientes (VAC, veyon-sync) comparan esta versión con la suya local
    para decidir si deben descargar el inventario actualizado. La versión
    tiene formato YYYYMMDDHHMMSS (timestamp UTC en el momento del último cambio).
    """
    ver = database.get_version()
    print(f"[VAS-VERSION] Consulta → {ver}", flush=True)
    return {"version": ver}


@app.get("/clients")
def list_clients():
    """
    Devuelve el inventario completo de clientes registrados.

    Cada entrada incluye: id, hostname, ip, mac, last_seen.
    Ordenado alfabéticamente por hostname.
    """
    try:
        clients = database.get_all_clients()
        print(f"[VAS-CLIENTS] Listado servido: {len(clients)} cliente(s)", flush=True)
        return {"clients": clients}
    except Exception as e:
        print(f"[VAS-ERROR] Fallo en GET /clients: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    """
    Devuelve los datos de un cliente específico por UUID.

    Retorna 404 si el UUID no existe en el registro.
    Útil para diagnóstico y para que servicios externos verifiquen
    si un equipo concreto está registrado.
    """
    print(f"[VAS-CLIENTS] Consulta individual: {client_id}", flush=True)
    client = database.get_client(client_id)

    if client is None:
        print(f"[VAS-CLIENTS] No encontrado: {client_id}", flush=True)
        raise HTTPException(status_code=404, detail="Client not found")

    print(
        f"[VAS-CLIENTS] Encontrado: {client_id} → "
        f"{client['hostname']} / {client['ip']}",
        flush=True,
    )
    return client
