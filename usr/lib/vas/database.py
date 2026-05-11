#!/usr/bin/env python3
import sqlite3
import os
import datetime

# Inyectadas por vas.py antes del primer uso
DB_PATH = None
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

    if not os.path.exists(VERSION_FILE):
        os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
        with open(VERSION_FILE, "w") as f:
            f.write("0")


def client_has_changed(client_id, hostname, ip, mac):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT hostname, ip, mac FROM clients WHERE id=?", (client_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return True

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
    cur.execute("SELECT id, hostname, ip, mac, last_seen FROM clients")
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0],
            "hostname": r[1],
            "ip": r[2],
            "mac": r[3],
            "last_seen": r[4],
        }
        for r in rows
    ]


def get_client(client_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, hostname, ip, mac, last_seen FROM clients WHERE id=?", (client_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return None

    return {"id": row[0], "hostname": row[1], "ip": row[2], "mac": row[3], "last_seen": row[4]}


def get_version() -> str:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"[VAS-DB] Aviso: archivo de versión no encontrado ({VERSION_FILE}). Usando 0.")
        return "0"
    except IOError as e:
        print(f"[VAS-DB] Error: no se puede leer versión ({VERSION_FILE}): {e}. Usando 0.")
        return "0"


def bump_version() -> str:
    version = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    tmp = VERSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(version)
        os.replace(tmp, VERSION_FILE)
    except IOError as e:
        print(f"[VAS-DB] Error: no se puede escribir versión ({VERSION_FILE}): {e}", flush=True)
    return version
