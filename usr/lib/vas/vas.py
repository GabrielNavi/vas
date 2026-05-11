#!/usr/bin/env python3
import os
import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import database

CONFIG_FILE = "/etc/vas/vas.conf"
CONFIG_DIR  = "/etc/vas/vas.conf.d"


def load_config():
    cfg = {
        "PORT":            "8000",
        "DB_PATH":         "/var/lib/vas/vas.db",
        "VERSION_FILE":    "/var/lib/vas/version",
        "CLIENT_TTL_DAYS": "30",
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
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                cfg[k] = v

    _apply_file(CONFIG_FILE)

    if os.path.isdir(CONFIG_DIR):
        for name in sorted(os.listdir(CONFIG_DIR)):
            if name.endswith(".conf"):
                _apply_file(os.path.join(CONFIG_DIR, name))

    return cfg


config = load_config()

database.DB_PATH      = config["DB_PATH"]
database.VERSION_FILE = config["VERSION_FILE"]


def validate_paths():
    errors = []
    for path, var_name in [
        (database.DB_PATH,      "DB_PATH"),
        (database.VERSION_FILE, "VERSION_FILE"),
    ]:
        dir_path = os.path.dirname(path)
        if not os.path.exists(dir_path):
            try:
                os.makedirs(dir_path, exist_ok=True)
                print(f"[VAS] Directorio creado: {dir_path}", flush=True)
            except OSError as e:
                errors.append(f"{var_name} ({path}): no se puede crear directorio {dir_path}: {e}")
                continue

        if not os.access(dir_path, os.W_OK):
            errors.append(f"{var_name} ({path}): sin permisos de escritura en {dir_path}")

        if os.path.exists(path) and not os.access(path, os.W_OK):
            errors.append(f"{var_name} ({path}): sin permisos de escritura en archivo")

    if errors:
        for err in errors:
            print(f"[VAS-ERROR] {err}", flush=True)
        raise RuntimeError(f"Configuración inválida: {len(errors)} problema(s) de permisos")

    print("[VAS] Validación de rutas completada.", flush=True)


try:
    validate_paths()
except RuntimeError as e:
    print(f"[VAS-ERROR] FATAL: {e}", flush=True)
    raise

database.init_db()


def cleanup_old_clients(days: int) -> int:
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"[VAS-CLEANUP] Eliminando clientes no vistos desde {cutoff_str}", flush=True)

    conn = database.get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM clients")
    total_before = cur.fetchone()[0]
    cur.execute("DELETE FROM clients WHERE last_seen < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    print(
        f"[VAS-CLEANUP] {deleted} cliente(s) eliminado(s), "
        f"{total_before - deleted} restante(s).",
        flush=True,
    )
    return deleted


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[VAS-STARTUP] Iniciando VAS en puerto {config.get('PORT', '8000')}", flush=True)
    try:
        ttl_days = int(config.get("CLIENT_TTL_DAYS", 30))
        print(f"[VAS-STARTUP] TTL configurado: {ttl_days} días", flush=True)
        deleted = cleanup_old_clients(ttl_days)
        if deleted > 0:
            version = database.bump_version()
            print(f"[VAS-STARTUP] Versión actualizada a {version} por limpieza", flush=True)
        print("[VAS-STARTUP] VAS inicializado correctamente.", flush=True)
    except Exception as e:
        print(f"[VAS-ERROR] Fallo en startup: {e}", flush=True)
    yield


app = FastAPI(lifespan=lifespan)


class Client(BaseModel):
    id: str
    hostname: str
    ip: str
    mac: Optional[str] = None


@app.post("/register")
def register(client: Client):
    try:
        mac = client.mac or ""

        changed = database.client_has_changed(client.id, client.hostname, client.ip, mac)
        database.add_or_update_client(client.id, client.hostname, client.ip, mac)

        if changed:
            print(
                f"[VAS-REGISTER] Cambios detectados en {client.id} ({client.hostname}) "
                f"IP={client.ip} MAC={mac or '(vacía)'}",
                flush=True,
            )
            version = database.bump_version()
            print(f"[VAS-REGISTER] Versión actualizada a {version}", flush=True)
        else:
            print(f"[VAS-REGISTER] {client.id} ({client.hostname}) sin cambios", flush=True)
            version = database.get_version()

        return {"status": "ok", "version": version}

    except Exception as e:
        print(f"[VAS-ERROR] Fallo en /register: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/version")
def version():
    ver = database.get_version()
    print(f"[VAS-VERSION] Consulta de versión: {ver}", flush=True)
    return {"version": ver}


@app.get("/clients")
def list_clients():
    try:
        clients = database.get_all_clients()
        print(f"[VAS-CLIENTS] Listado de {len(clients)} cliente(s)", flush=True)
        return {"clients": clients}
    except Exception as e:
        print(f"[VAS-ERROR] Fallo en /clients: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    client = database.get_client(client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client
