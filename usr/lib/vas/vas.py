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
        "VEYON_CSV_PATH": "/var/lib/vas/computers-master.csv"
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

# Inyectar rutas en database.py
database.DB_PATH = config["DB_PATH"]
database.JSON_PATH = config["CONFIG_PATH"]
database.VERSION_FILE = config["VERSION_FILE"]

# Inicializar DB y versión
database.init_db()


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
        return

    clients = database.get_all_clients()
    csv_path = config["VEYON_CSV_PATH"]
    location = (config["VEYON_LOCATION"] or "Autoregistrados").replace(";", "")

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", encoding="utf-8") as f:
        for c in clients:
            hostname = (c.get("hostname") or "").replace(";", "")
            ip = (c.get("ip") or "").replace(";", "")
            mac = (c.get("mac") or "").replace(";", "")
            f.write(f"computer;{hostname};{ip};{mac};{location}\n")

    # Refrescamos solo la location administrada por VAS para no afectar otras.
    subprocess.run(
        ["veyon-cli", "networkobjects", "remove", location],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

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
    )


@app.post("/register")
def register(client: Client):
    try:
        mac = client.mac or ""

        changed = database.client_has_changed(
            client.id, client.hostname, client.ip, mac
        )

        database.add_or_update_client(client.id, client.hostname, client.ip, mac)

        if changed:
            database.regenerate_json()
            try:
                sync_veyon_master()
            except Exception as e:
                # El autoregistro no debe fallar por un problema puntual de Veyon.
                print(f"[VAS] Aviso: fallo al sincronizar con Veyon Master: {e}")
            version = database.bump_version()
        else:
            version = database.get_version()

        return {"status": "ok", "version": version}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/version")
def version():
    return {"version": database.get_version()}


@app.get("/config")
def config_file():
    try:
        with open(config["CONFIG_PATH"]) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Config not found")


@app.on_event("startup")
def startup_sync() -> None:
    # En arranque sincronizamos para asegurar estado consistente en Veyon Master.
    try:
        database.regenerate_json()
        sync_veyon_master()
    except Exception:
        # No bloqueamos el arranque del servicio por fallos de integración opcional.
        pass
