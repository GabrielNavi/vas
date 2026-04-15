#!/usr/bin/env python3
import os
import json
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import database

CONFIG_FILE = "/etc/vas/vas.conf"


# -----------------------------
# Cargar configuración
# -----------------------------
def load_config():
    cfg = {
        "PORT": 8000,
        "DB_PATH": "/var/lib/vas/vas.db",
        "CONFIG_PATH": "/var/lib/vas/computers.json",
        "VERSION_FILE": "/var/lib/vas/version"
    }

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    cfg[k] = v

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
