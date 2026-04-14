#!/usr/bin/env python3
import sqlite3
import json
import os

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
    try:
        with open(VERSION_FILE) as f:
            return int(f.read().strip())
    except:
        return 0


def bump_version():
    v = get_version() + 1
    with open(VERSION_FILE, "w") as f:
        f.write(str(v))
    return v
