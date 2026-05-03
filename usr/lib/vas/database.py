#!/usr/bin/env python3
import sqlite3
import json
import os
import datetime

# Estas variables serán inyectadas por vas.py
DB_PATH = None
JSON_PATH = None
VERSION_FILE = None


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            hostname TEXT,
            ip TEXT,
            mac TEXT,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

    # Inicializar versión si no existe
    if not os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "w") as f:
            f.write("0")


def client_has_changed(client_id, hostname, ip, mac):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT hostname, ip, mac FROM clients WHERE id=?", (client_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return True  # nuevo cliente

    old_hostname, old_ip, old_mac = row
    return (hostname != old_hostname) or (ip != old_ip) or (mac != old_mac)


def add_or_update_client(client_id, hostname, ip, mac):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO clients (id, hostname, ip, mac)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            hostname=excluded.hostname,
            ip=excluded.ip,
            mac=excluded.mac,
            last_seen=CURRENT_TIMESTAMP
    """, (client_id, hostname, ip, mac))

    conn.commit()
    conn.close()


def get_all_clients():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, hostname, ip, mac FROM clients")
    rows = cur.fetchall()
    conn.close()

    return [
        {"id": r[0], "hostname": r[1], "ip": r[2], "mac": r[3]}
        for r in rows
    ]


def regenerate_json():
    clients = get_all_clients()
    data = {"computers": clients}

    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)


# -------------------------
# Gestión de versión
# -------------------------

def get_version():
    """Lee versión del archivo. Retorna 0 si no existe o es inválida."""
    try:
        with open(VERSION_FILE) as f:
            content = f.read().strip()
            return int(content)
    except FileNotFoundError:
        print(f"[VAS-DB] Aviso: archivo de versión no encontrado ({VERSION_FILE}). Usando valor 0.")
        return 0
    except ValueError as e:
        print(f"[VAS-DB] Error: contenido inválido en versión file ({VERSION_FILE}): {e}. Usando valor 0.")
        return 0
    except IOError as e:
        print(f"[VAS-DB] Error: no se puede leer versión file ({VERSION_FILE}): {e}. Usando valor 0.")
        return 0


def bump_version():
    """Genera nueva versión con timestamp UTC y la escribe al archivo."""
    version = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    try:
        with open(VERSION_FILE, "w") as f:
            f.write(version)
    except IOError as e:
        print(f"[VAS-DB] Error: no se puede escribir versión file ({VERSION_FILE}): {e}", flush=True)
    return version
