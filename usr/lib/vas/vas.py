#!/usr/bin/env python3
import os
import json
import shutil
import subprocess
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import database

CONFIG_FILE = "/etc/vas/vas.conf"
CONFIG_DIR = "/etc/vas/vas.conf.d"


# -----------------------------
# Cargar configuración
# -----------------------------
def load_config():
    cfg = {
        "PORT": 8000,
        "DB_PATH": "/var/lib/vas/vas.db",
        "CONFIG_PATH": "/var/lib/vas/computers.json",
        "VERSION_FILE": "/var/lib/vas/version",
        "VEYON_MASTER_SYNC": "1",
        "VEYON_LOCATION": "Autoregistrados",
        "VEYON_CSV_PATH": "/var/lib/vas/computers-master.csv",
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

    # Config principal
    _apply_file(CONFIG_FILE)

    # Overlays en orden lexical
    if os.path.isdir(CONFIG_DIR):
        for name in sorted(os.listdir(CONFIG_DIR)):
            if name.endswith(".conf"):
                _apply_file(os.path.join(CONFIG_DIR, name))

    return cfg



config = load_config()

# Inyectar rutas en database.py
database.DB_PATH = config["DB_PATH"]
database.JSON_PATH = config["CONFIG_PATH"]
database.VERSION_FILE = config["VERSION_FILE"]

import datetime

def validate_paths():
    """Valida permisos de acceso a rutas críticas antes de startup."""
    paths_to_check = [
        (database.DB_PATH, "DB_PATH", True),
        (database.JSON_PATH, "JSON_PATH", True),
        (database.VERSION_FILE, "VERSION_FILE", True),
        (config["VEYON_CSV_PATH"], "VEYON_CSV_PATH", True),
    ]
    
    errors = []
    for path, var_name, needs_write in paths_to_check:
        dir_path = os.path.dirname(path)
        if not os.path.exists(dir_path):
            try:
                os.makedirs(dir_path, exist_ok=True)
                print(f"[VAS] Directorio creado: {dir_path}")
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
        raise RuntimeError(f"Configuración inválida: {len(errors)} problemas de permisos detectados")
    
    print(f"[VAS] Validación de rutas completada: todas las rutas están accesibles", flush=True)

try:
    validate_paths()
except RuntimeError as e:
    print(f"[VAS-ERROR] FATAL: {e}", flush=True)
    raise

# Inicializar DB y versión
database.init_db()

def cleanup_old_clients(days: int) -> int:
    """Elimina clientes cuyo last_seen sea más antiguo que X días."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Contar clientes antes de eliminación
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM clients")
    total_before = cur.fetchone()[0]
    cur.close()
    conn.close()
    
    print(f"[VAS-CLEANUP] Iniciando limpieza: eliminando clientes no vistos después de {cutoff_str}", flush=True)

    conn = database.get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM clients WHERE last_seen < ?", (cutoff,))
    deleted = cur.rowcount

    conn.commit()
    conn.close()
    
    total_after = total_before - deleted
    print(f"[VAS-CLEANUP] Completado: {deleted} cliente(s) eliminado(s), {total_after} cliente(s) restante(s)", flush=True)

    return deleted


# -----------------------------
# FastAPI
# -----------------------------
app = FastAPI()


class Client(BaseModel):
    id: str
    hostname: str
    ip: str
    mac: Optional[str] = None


def _is_enabled(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def sync_veyon_master() -> None:
    if not _is_enabled(config.get("VEYON_MASTER_SYNC", "1")):
        return

    if shutil.which("veyon-cli") is None:
        print(f"[VAS-VEYON] Aviso: veyon-cli no encontrado. Omitiendo sincronización.", flush=True)
        return

    clients = database.get_all_clients()
    csv_path = config["VEYON_CSV_PATH"]
    location = (config["VEYON_LOCATION"] or "Autoregistrados").replace(";", "")

    print(f"[VAS-VEYON] Generando CSV con {len(clients)} cliente(s) para location '{location}'", flush=True)

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", encoding="utf-8") as f:
        for c in clients:
            hostname = (c.get("hostname") or "").replace(";", "")
            ip = (c.get("ip") or "").replace(";", "")
            mac = (c.get("mac") or "").replace(";", "")
            f.write(f"computer;{hostname};{ip};{mac};{location}\n")

    print(f"[VAS-VEYON] Limpiando location existente: {location}", flush=True)
    # Refrescamos solo la location administrada por VAS para no afectar otras.
    subprocess.run(
        ["veyon-cli", "networkobjects", "remove", location],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print(f"[VAS-VEYON] Importando {len(clients)} cliente(s) en Veyon", flush=True)
    try:
        subprocess.run(
            [
                "veyon-cli",
                "networkobjects",
                "import",
                csv_path,
                "format",
                "%type%;%name%;%host%;%mac%;%location%",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[VAS-VEYON] Sincronización completada exitosamente", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"[VAS-ERROR] Fallo en sincronización de Veyon: {e}", flush=True)
        raise


@app.post("/register")
def register(client: Client):
    try:
        mac = client.mac or ""

        changed = database.client_has_changed(
            client.id, client.hostname, client.ip, mac
        )

        database.add_or_update_client(client.id, client.hostname, client.ip, mac)

        if changed:
            action = "actualizado"
            print(f"[VAS-REGISTER] Cambios detectados en cliente {client.id} ({client.hostname})", flush=True)
            print(f"[VAS-REGISTER]   IP: {client.ip}, MAC: {mac or '(vacía)'}", flush=True)
            database.regenerate_json()
            try:
                sync_veyon_master()
            except Exception as e:
                # El autoregistro no debe fallar por un problema puntual de Veyon.
                print(f"[VAS-ERROR] Fallo al sincronizar con Veyon Master: {e}", flush=True)
            version = database.bump_version()
            print(f"[VAS-REGISTER] Versión actualizada a {version}", flush=True)
        else:
            action = "sin cambios"
            print(f"[VAS-REGISTER] Cliente {client.id} ({client.hostname}) registrado sin cambios", flush=True)
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


@app.get("/config")
def config_file():
    try:
        print(f"[VAS-CONFIG] Sirviendo configuración desde {config['CONFIG_PATH']}", flush=True)
        with open(config["CONFIG_PATH"]) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[VAS-ERROR] Archivo de configuración no encontrado: {config['CONFIG_PATH']}", flush=True)
        raise HTTPException(status_code=404, detail="Config not found")


@app.on_event("startup")
def startup_sync() -> None:
    print(f"[VAS-STARTUP] Iniciando VAS en puerto {config.get('PORT', 8000)}", flush=True)
    try:
        # Limpieza automática de clientes antiguos
        ttl_days = int(config.get("CLIENT_TTL_DAYS", 30))
        print(f"[VAS-STARTUP] TTL configurado: {ttl_days} días", flush=True)
        deleted = cleanup_old_clients(ttl_days)

        # Regenerar JSON y sincronizar Veyon
        print(f"[VAS-STARTUP] Regenerando JSON de configuración", flush=True)
        database.regenerate_json()
        
        if deleted > 0:
            version = database.bump_version()
            print(f"[VAS-STARTUP] Versión actualizada a {version} por limpieza", flush=True)
        
        print(f"[VAS-STARTUP] Iniciando sincronización de Veyon Master", flush=True)
        sync_veyon_master()
        
        print(f"[VAS-STARTUP] VAS inicializado exitosamente", flush=True)

    except Exception as e:
        print(f"[VAS-ERROR] Fallo en startup_sync(): {e}", flush=True)
        # No bloqueamos el arranque del servicio por fallos de integración opcional.
        print(f"[VAS-STARTUP] Continuando a pesar del error (integración opcional)", flush=True)
